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

import json
import logging
import os
import re
import stat
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator

import clone_identity  # which project a network git op belongs to, for the node's credential helper
import host_platform
from jaah_env import env_get  # renamed env vars
import runner_env   # docker_env(): the node-managed engine's endpoint
import session_user   # which tmux server a client call must name (`-S`)
import slot_repos     # a multi-repo slot's other trees: the clone that owns a slot tree, removed with it on erase
import slot_clone     # a standalone forge clone: the preflight's throwaway checkout
import slot_residue   # what a slot leaves outside its tree: its scratch dirs

from commands import EXECUTE_TIMEOUT_SECONDS, native_command
import env_helpers
import environment
from env_helpers import _tmux_safe_env, jaah_home

log = logging.getLogger("jaahd.worktree_ops")

# Read through the accessors below so a test can monkeypatch `_REPO_ROOT`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# The NON-agent windows a slot can also have (`console.py`'s plain-shell and web-console tabs). Both are
# resolved BY NAME, so a teardown must skip them explicitly.
_NON_AGENT_WINDOW_SUFFIXES = ("|term", "|web")
#: A docker compose project name (`docker compose -p`): lowercase alphanumerics, `-` and `_`, as the CP mints them
#: (`p<project>-<number>`, `app/slot_allocation.py::_mint_destroy`).
_COMPOSE_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def _panes_per_socket(run) -> Iterator[tuple[str, list[tuple[str, str, str]]]]:
    """Every live tmux server's panes, `(socket, [(pane cwd, window_id, window_name), …])` per socket — the one
    `list-panes` read behind `_agent_windows_under`. An absent socket path is skipped
    rather than probed: a tmux client with no server starts one AS ITSELF; a server that does not answer is
    skipped too."""
    for socket in session_user.manage_sockets():
        if socket and not os.path.exists(socket):
            continue
        reply = run(*session_user.tmux_cmd(
            socket, "list-panes", "-a", "-F", "#{pane_current_path}\t#{window_id}\t#{window_name}"),
            env=_tmux_safe_env())
        if reply.get("exit_code") != 0:
            continue
        panes = []
        for line in (reply.get("stdout") or "").splitlines():
            pane_path, _, rest = line.partition("\t")
            target, _, name = rest.partition("\t")
            if pane_path and target:
                panes.append((pane_path, target, name))
        yield socket, panes


def _agent_windows_under(path: str, run) -> list[tuple[str, str]]:
    """EVERY distinct `(socket, window_id)` with a pane at — or under — `path`, minus the non-agent tabs.

    A teardown needs them all: `kill_gates`' `expect_cwd` fence cannot tell sibling windows in one tree apart. The same set as
    `reconcile/context.py::wt_tmux_targets` (which needs a pass snapshot this sync command lacks). Scans BOTH
    sockets: during an in-place-upgrade drain a tree's windows can be split across servers."""
    if not path:
        return []
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for socket, panes in _panes_per_socket(run):
        for pane_path, target, name in panes:
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
# under the uid switch the ports op is delegated with `_PORT_GRID_ENV_KEYS` (`_PORT_GRID_OPS`).
_CTL_BUS_OPS = frozenset({"ports_audit", "doctor"})

# The op that shells the pool port audit, which reads every pool slot's `.env` — `0600 jaah:jaah`,
# holding real secrets — so it is EACCES for uid `jaahd` and is delegated to the session user.
_PORT_GRID_OPS = frozenset({"ports_audit"})

# What a DELEGATED port-grid run may carry — an allowlist, since tmux `run-shell` otherwise hands over
# its own frozen env. `HOME`/`PATH` absent: the seam fills the session user's. JAAH_HOME is required
# (resolve_clone_root has no default); the socket overrides only if non-default; locale/TZ because the
# script prints non-ASCII. NODE_TOKEN and the CP base are WITHHELD: every read it makes is a socket verb
# on the `0660` group-`jaah` credential socket, and the token is the one secret
# `jaah-runner@.service` refuses to list.
_PORT_GRID_ENV_KEYS: tuple[str, ...] = (
    "JAAH_HOME", "JAAH_CRED_SOCKET", "JAAHD_CTL", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)


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


def _hold_erase_lock(project: str, number: int, who: str) -> tuple:
    """The slot's erase lock, taken without waiting: `(lock file, None)`, or `(None, the refusal)` — the command
    result naming why not, prefixed `who`."""
    locks_dir = env_helpers.fleet_locks_dir()
    locks_err = _ensure_locks_dir(locks_dir)
    if locks_err:
        return None, {"exit_code": 1, "stdout": "", "stderr": f"{who}: could not create {locks_dir}: {locks_err}"}
    try:
        lock_fh = open_erase_lock(project, number)
    except OSError as exc:
        # ELOOP: something planted a symlink where the lock lives.
        return None, {"exit_code": 1, "stdout": "", "stderr": f"{who}: cannot open this slot's lock ({exc}) — refusing"}
    try:
        host_platform.lock(lock_fh, blocking=False)
    except OSError:
        lock_fh.close()
        return None, {"exit_code": 1, "stdout": "", "stderr": "another worktree operation is in progress for this slot"}
    return lock_fh, None


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

    from worktree_mechanics.core import slot_path

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
        # No lock: a retry must converge.
        if number != 0:
            # A multi-repo slot's other trees, their stacks and scratch, and its manifest (`slot_repos`).
            slot_repos.release_slot_repos(str(clone_resolved), number)
        slot_residue.purge_scratch(str(resolved))
        return {"exit_code": 0,
                "stdout": json.dumps({"deleted": True, "already_absent": True, "path": str(resolved)}),
                "stderr": ""}

    lock_fh, refused = _hold_erase_lock(project, number, "worktree_erase")
    if refused:
        return refused

    try:
        wt = str(resolved)

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
            # The tree is a standalone clone (`slot_clone`): removing the directory is the whole erase, dirty or
            # not — the erase never refuses.
            if host_platform.is_windows():
                # The runner account removes its own tree there: the daemon deletes nothing under the
                # runner-writable home itself.
                err = session_user.remove_tree_as_session_user(str(resolved))
                if err:
                    raise OSError(err)
            elif session_user.switch_needed():
                # Under the uid split the session user owns the tree, and its tools make `2755` directories
                # (npm's `node_modules`) that the daemon, only in the group, cannot empty: the owner removes it.
                # What it leaves (root-owned residue) the daemon tries next, so the hint below still names it.
                err = session_user.remove_tree_as_session_user(str(resolved))
                if err:
                    log.warning("worktree_erase %s #%s: the session user's delete failed (%s) — trying as "
                                "the daemon", project, number, err)
                    shutil.rmtree(resolved, ignore_errors=False)
            else:
                shutil.rmtree(resolved, ignore_errors=False)
            if number != 0:
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
    from worktree_mechanics.core import slot_path  # noqa: PLC0415 — deferred, as every caller here

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
        return {"exit_code": 0,
                "stdout": json.dumps({"deleted": True, "already_absent": True, "path": str(resolved)}),
                "stderr": ""}

    lock_fh, refused = _hold_erase_lock(project, number, "folder_erase")
    if refused:
        return refused
    try:
        err = _remove_folder(str(resolved))
        if err:
            return {"exit_code": 1, "stdout": "", "stderr": f"failed to erase folder {resolved}: {err}"}
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
    that would report the audit as having run."""
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    if not session_user.switch_needed():
        # No uid split: in process with the daemon's real env, like every other `_CTL_BUS_OPS` op.
        return run(*argv, env=None)
    if not session_user.can_run_as_session_user():
        return {"exit_code": SWITCH_UNAVAILABLE_RC, "stdout": "",
                "stderr": ("port grid: session-user switch required but unavailable — refusing to run "
                           "the pool port audit as the daemon uid, which cannot read "
                           "a pool slot's 0600 jaah:jaah .env; nothing was audited. Check jaah-runner@<node name>.service")}
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
    }
    if project:
        ops["ports_audit"] = [_python(), _check_pool_ports(), project]
    argv = ops.get(op)
    if argv is None:
        return {"exit_code": -1, "stdout": "",
                "stderr": f"unknown or unavailable run op: {op!r}; allowed: {sorted(ops)}"}
    # The port-grid op needs the session-user seam (see `_PORT_GRID_OPS`).
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


# Git's refname rules (single-homed in `slot_repos`), also applied to a branch name below.
_BAD_PREFIX_CHARS = slot_repos.BAD_REF_CHARS


# ── Trigger preflight (the "Repo script" trigger) ───────────────────────────────────────────────────────
#
# CP-INTERNAL, like `worktree_erase`: absent from `app/routes/commands.py`'s `VALID_TYPES`, so only the
# trigger processor (`app/trigger_preflight_script.py`) reaches it. It runs the OWNER'S script from the
# project's repo — the trust the merge gate already extends to the gate command — against the integration branch,
# in a throwaway shallow clone `<state>/preflight/<uuid>` made from the forge for this run alone and removed after
# it, whatever happened: no `worktrees` row, never leased, so no pool slot is touched.
#
# The clone spends the trigger OWNER'S git login: it runs inside `clone_identity.acting_for` a `trigger` subject
# carrying the CP's grant, which the credential broker trades at the CP's trigger arm (`cred_broker._resolve_trigger`).
# The script then runs as the session user, with no credential admitted and a curated env (`_gate_env()` plus the
# input path); every git call pins repo-local code off (`git_env.NO_REPO_CODE`, `worktree_mechanics.core._run_git`).

#: The env var naming the file that holds the preflight input (`{"event","state","seen_keys"}`), also on stdin.
PREFLIGHT_INPUT_ENV = "JAAH_PREFLIGHT_INPUT"
#: One byte over the CP's 64 KiB output cap, so an oversized answer still reads as "over 64 KiB" there.
PREFLIGHT_STDOUT_CAP = 64 * 1024 + 1
_PREFLIGHT_STDERR_LINE_CAP = 500
# A script without its executable bit still runs when its suffix names the interpreter (`.py`: the node's own,
# `runner_env.python_cmd()`, resolved at call time).
_PREFLIGHT_INTERPRETERS = {".sh": "bash", ".js": "node", ".mjs": "node"}
# Copies stdin to a private per-run file (`mktemp` is 0600), names it in the env, runs the script with that
# file as its stdin, and removes it on exit: the event must not outlive the run on disk.
_PREFLIGHT_WRAPPER = (
    f'F=$(mktemp) || exit 111; trap \'rm -f "$F"\' EXIT; trap \'exit 143\' TERM INT; '
    f'export {PREFLIGHT_INPUT_ENV}="$F"; cat >"$F" || exit 111; "$@" <"$F"'
)


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
    never reaches a path, an argv or a URL. A CP older than the grant sends none, and is refused: this node will
    not clone with any other login."""
    branch = str(args.get("integration_branch") or "").strip()
    script = str(args.get("script_path") or "").strip()
    clone_url = str(args.get("clone_url") or "").strip()
    grant = str(args.get("grant") or "")
    try:
        timeout = int(args.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        timeout = 0
    if not clone_identity.GRANT_RE.match(grant):
        return {}, "needs grant (the control plane's clone authorization; this control plane is older than the node)"
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
    return {"clone_url": clone_url, "branch": branch, "script": script, "timeout": timeout, "input": args["input"],
            "grant": grant}, ""


class _OutOfTime(Exception):
    """The run's whole budget (clone + script) is spent."""


def _left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise _OutOfTime
    return remaining


@native_command("trigger_preflight")
def trigger_preflight(command_type: str, args: dict, run) -> dict:
    """Clone the integration branch (depth 1) into a fresh directory, run the script there, remove the clone, and
    return `{exit_code, stdout (≤64 KiB + 1), stderr (its first line)}`. A reply flagged `unavailable` (session-user
    switch down) is the node's "not now", never the script's "no"; one flagged `setup` failed before the script ran
    (a clone failure names the forge's host and the cause); `timed_out` spent the whole `timeout_seconds` budget,
    which the clone and the script share so the CP's wait is never outrun."""
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    parsed, why = _preflight_args(args)
    if why:
        return _preflight_result(1, f"trigger_preflight {why}; nothing was run", setup=True)
    deadline = time.monotonic() + parsed["timeout"]
    if session_user.switch_needed() and not session_user.can_run_as_session_user():
        return _preflight_result(
            SWITCH_UNAVAILABLE_RC, "the node cannot switch to its session user right now", unavailable=True)
    checkout = str(env_helpers.state_dir("preflight", uuid.uuid4().hex))
    try:
        refused = _clone_for_preflight(parsed, checkout, deadline)
        if refused:
            return refused
        return _run_preflight_script(parsed, checkout, deadline)
    except _OutOfTime:
        return _timed_out(parsed)
    finally:
        _remove_preflight_clone(checkout)


def _timed_out(parsed: dict) -> dict:
    return _preflight_result(124, f"timed out after {parsed['timeout']}s", timed_out=True)


def _clone_for_preflight(parsed: dict, checkout: str, deadline: float) -> dict | None:
    """Clone `clone_url` at `origin/<branch>`, depth 1, as the trigger's owner. None when ready, else the `setup`
    failure. Raises `_OutOfTime` when the budget ran out under it."""
    from worktree_mechanics.core import ResetRefused

    acting = clone_identity.Acting(kind=clone_identity.ACTING_TRIGGER, id=parsed["grant"])
    try:
        slot_clone.clone(checkout, parsed["clone_url"], parsed["branch"], acting, depth=1, timeout=_left(deadline))
    except slot_clone.CloneFailed as exc:
        _left(deadline)   # a clone cut off by the budget is a timeout, not a forge fault
        return _preflight_result(1, f"preflight {exc}", setup=True)
    except ResetRefused as exc:
        return _preflight_result(1, f"preflight clone: {_first_line(str(exc))}", setup=True)
    return None


def _remove_preflight_clone(checkout: str) -> None:
    """Remove the run's clone (and a staging dir a cut-off clone left); a failure is logged, never raised: the
    reply is already decided."""
    from worktree_mechanics.core import ResetRefused

    for path in (checkout, checkout + slot_clone.STAGING_SUFFIX):
        try:
            slot_clone.erase(path)
        except ResetRefused as exc:
            log.warning("trigger_preflight: could not remove %s: %s", path, exc)


def _unadmitted(argv: list[str], *, cwd: str | None, timeout: float, stdin: str | None = None,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """ARGV as the session user (or, with no uid split, in its own process group) with a curated env and NO
    credential admitted. Raises `TimeoutExpired`, and `RuntimeError` when the switch went away."""
    env = {**_gate_env(), **(env or {})}
    if session_user.switch_needed():
        # Nothing on the session user's channel is admitted to the credential helper. The far side's `timeout` fires 5s
        # inside the deadline it is given, so the budget handed over is `timeout + 5`.
        return session_user.run_as_session_user(argv, cwd=cwd, env=env, timeout=timeout + 5, input=stdin)
    return session_user.run_bounded(argv, input=stdin or "", cwd=cwd, timeout=timeout, kill_group=True, env={
        **env, "HOME": os.environ.get("HOME") or os.path.expanduser("~"),
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"})


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


@native_command("sync_access_guard")
def sync_access_guard(command_type: str, args: dict, run) -> dict:
    # Re-install the git-native push guard with the declared branch prefix and push flag,
    # fired by `PUT /api/projects/{id}`. The project's directory is derived from this node's own home and
    # `project_id` (`core.project_anchor`), never a path the frame names. Idempotent; revert is not an
    # uninstall — empty prefix + push enabled is the declared unrestricted pair, a no-op hook.
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC, project_anchor

    raw_pid = args.get("project_id")
    clone_path = (project_anchor(environment.pool_root_or_empty(), raw_pid)
                  if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else "")
    if not clone_path:
        return {"exit_code": 1, "stdout": "",
                "stderr": "sync_access_guard needs project_id and this node's Jaah home"}
    # PRESENCE, not truthiness: `""` is a legitimate prefix value, so an absent key cannot mean it.
    if "branch_prefix" not in args:
        return {"exit_code": 1, "stdout": "",
                "stderr": ("sync_access_guard needs branch_prefix (the project's, sent by the CP; '' is a "
                           "legal value, an absent key is not)")}
    branch_prefix = str(args.get("branch_prefix") or "")
    if not slot_repos.legal_ref_prefix(branch_prefix):
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

    # `integration_branch` names where work goes in a refusal and, with `allow_force_push`, the one branch that
    # lets through; the anchor's is the primary's. `allow_force_push` is not a real boolean only from a CP older
    # than it: False (absent reads as refused), never inferred open.
    integration_branch = str(args.get("integration_branch") or "")
    allow_force_push = args.get("allow_force_push") is True
    # The project's `jaah_repo_prefix` (project-level: every clone takes it). Absent — an older CP — is None, which
    # leaves each clone's marker as it is; `""` clears it. Anything but a string is refused, never guessed.
    jaah_repo_prefix = args.get("jaah_repo_prefix")
    if jaah_repo_prefix is not None and not isinstance(jaah_repo_prefix, str):
        return {"exit_code": 1, "stdout": "",
                "stderr": ("sync_access_guard: jaah_repo_prefix must be a string "
                           f"(got {type(jaah_repo_prefix).__name__})")}
    # Every repository directory of the project is its own clone (`slot_clone`), so each carries the guard: a
    # slot's primary takes the top-level values, a `_repos/<rid>/<N>` clone ITS repository's from `repos[]`, else
    # (an older CP, which sent project-level values only) the top-level pair with no integration branch and force
    # push refused. The derived anchor only locates the project's directory.
    per_repo = _per_repo_guard(args.get("repos"))
    if isinstance(per_repo, str):
        return {"exit_code": 1, "stdout": "", "stderr": f"sync_access_guard: {per_repo}"}
    primary_values = (branch_prefix, push_enabled, integration_branch, allow_force_push)
    clones = _project_clones(os.path.dirname(str(clone_path).rstrip("/")))
    if not clones:
        return {"exit_code": 0, "stdout": "no clone of this project on this node — nothing to guard", "stderr": ""}
    results = []
    for clone, rid in clones:
        fallback = primary_values if rid is None else (branch_prefix, push_enabled, "", False)
        argv = access_guard_argv(clone, *(per_repo.get(rid, fallback) if rid is not None else fallback),
                                 jaah_repo_prefix=jaah_repo_prefix)
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
            "stdout": "\n".join(f"{c}: {(r.get('stdout') or '').strip()}"
                               for (c, _rid), r in zip(clones, results, strict=False))[-8000:],
            "stderr": "\n".join(f"{c}: {(r.get('stderr') or '').strip()}"
                               for (c, _rid), r in zip(clones, results, strict=False)
                                if r.get("stderr"))[-2000:]}


def _per_repo_guard(raw: object) -> dict[int, tuple[str, bool, str, bool]] | str:
    """`repos[]` -> {repo_id: (branch_prefix, push_enabled, integration_branch, allow_force_push)}, or the
    refusal text for a row that names a repository but carries an unusable value (never guessed open). An
    absent list (an older CP) is `{}`."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        return f"repos must be a list (got {type(raw).__name__})"
    out: dict[int, tuple[str, bool, str, bool]] = {}
    for row in raw:
        rid = row.get("repo_id") if isinstance(row, dict) else None
        if not isinstance(rid, int) or isinstance(rid, bool):
            return f"repos row without an integer repo_id: {row!r}"
        prefix, push, force = row.get("branch_prefix"), row.get("push_enabled"), row.get("allow_force_push")
        if not isinstance(prefix, str) or not slot_repos.legal_ref_prefix(prefix):
            return f"repository {rid}: '{prefix}' is not a legal git ref prefix"
        if not isinstance(push, bool) or not isinstance(force, bool):
            return f"repository {rid}: push_enabled and allow_force_push must be booleans"
        out[rid] = (prefix, push, str(row.get("integration_branch") or ""), force)
    return out


def access_guard_argv(clone: str, branch_prefix: str, push_enabled: bool, integration_branch: str = "",
                      allow_force_push: bool | None = None, protect_integration: bool = False,
                      jaah_repo_prefix: str | None = None) -> list[str]:
    """The installer argv for one clone. Flags, not positionals: an empty positional prefix is easily
    dropped, and `--push-enabled true|false` must never be readable from absence. `allow_force_push=None`
    and `jaah_repo_prefix=None` leave the clone's marker as it is (`""` clears the latter); `protect_integration`
    False writes nothing (the marker only adds)."""
    argv = [_python(), _install_access_guard(), clone,
            "--branch-prefix", branch_prefix,
            "--push-enabled", "true" if push_enabled else "false"]
    if integration_branch:
        argv += ["--integration-branch", integration_branch]
    if allow_force_push is not None:
        argv += ["--allow-force-push", "true" if allow_force_push else "false"]
    if protect_integration:
        argv += ["--protect-integration", "true"]
    if jaah_repo_prefix is not None:
        argv += ["--jaah-repo-prefix", jaah_repo_prefix]
    return argv


def _project_clones(project_dir: str) -> list[tuple[str, int | None]]:
    """`(path, repo id)` of every standalone clone of the project at `project_dir` (`<home>/repos/<pid>`): each
    slot `<N>` (the primary: None) and each `_repos/<rid>/<N>` (repository `<rid>`), static and dynamic alike —
    and a retired `main` (or `_repos/<rid>/main`) still on disk, whose config its held linked worktrees share. A directory whose `.git` is
    not a real directory (a symlink, a linked worktree's pointer file) is no clone of its own and is skipped."""
    def standalone(path: str) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(os.path.join(path, ".git")).st_mode) and not os.path.islink(path)
        except OSError:
            return False

    def names(path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    out: list[tuple[str, int | None]] = [
        (path, None) for name in names(project_dir)
        if (name.isdigit() or name == "main") and standalone(path := os.path.join(project_dir, name))]
    root = os.path.join(project_dir, slot_repos.NON_PRIMARY_DIRNAME)
    for rid in names(root):
        if rid.isdigit() and not os.path.islink(os.path.join(root, rid)):
            out += [(path, int(rid)) for n in names(os.path.join(root, rid))
                    if (n.isdigit() or n == "main") and standalone(path := os.path.join(root, rid, n))]
    return out
