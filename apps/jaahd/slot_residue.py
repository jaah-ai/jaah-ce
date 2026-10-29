"""What a pool slot leaves on the node OUTSIDE its tree, and how it goes — one home for both ends.

An erase removes the tree (`worktree_ops.worktree_erase`); nothing the slot made may outlive it. Outside the
tree a slot leaves:

  * its compose stack: containers, network, volumes and the images compose built for it
    (`slot_destroy.compose_down`, run by the erase while the tree's `.env` still names the project);
  * Claude Code's per-cwd scratch, `/tmp/claude-<uid>/<munged cwd>` (`scratch_dirs_for`, run by the erase);
  * task-keyed staging dirs (`/tmp/cockpit-task-{reports,attachments,inputs}/task-<id>`), a runner's own
    task-keyed scratch directly in `/tmp` (`/tmp/task-<id>-shots`, `/tmp/task-<id>-quick.log`) and pytest's
    per-user `/tmp/pytest-of-<user>/` runs, which no slot path names, so only the sweep can judge them.

`sweep()` (the `slot-residue-sweep` ScheduledTask, `main.py --sweep-slot-residue`) collects what an erase
missed or what predates this module: scratch and stacks of slot directories that no longer exist, task dirs
no slot or live runner on this node holds, and stale pytest runs.

Every fence fails closed — an unreadable listing removes nothing of that kind:
  * scratch: only names under the munge of `<jaah_home>/repos/`, and never one a slot directory on disk
    munges to (or is a subdirectory of);
  * stacks: only `p<project>` / `p<project>-<n>` names (`worktree_mechanics.ports.stack_base`), never one a slot
    `.env` names (`disk_reclaim.live_projects`, None = skip) nor one whose slot directory exists;
  * task dirs: only `task-<digits>` (in `/tmp` itself `task-<digits>-<name>`, and only the session user's),
    never one held by a slot row or a live runner, never one touched in the last `STALE_SECONDS`.

Shared caches (`~/.npm`, `~/.cache/ms-playwright*`) are NOT swept: live slots read them, they are keyed by
content or browser version rather than by slot, and Playwright's own install drops browser versions no
installed package references. Deletes run as the session user, who owns all of it. Linux/macOS only.

`transfer_purge()` (the `org_transfer_purge` node verb, sent once by the node-move cleanup,
`app/durable/node_transfer.py`) is the exception: the node now serves another org, whose runner runs as the
same session uid, so what the old org left readable to it goes AT ONCE, with no staleness window. Its guarantee
is NAMED RESIDUE — it removes the list below, not "nothing readable":
  * every task dir the new org does not hold (the CP names those) and the whole pytest root;
  * every Claude Code scratch dir no slot directory of the new org's stacks claims (`keep`, mapped as
    `orphan_stacks` maps a stack to its slot);
  * the shared caches above, pip's, uv's, cargo's and Go's (`TRANSFER_CACHE_DIRS`: npm's may hold
    private-package tarballs);
  * the credential and persistence dotfiles (`TRANSFER_DOTFILES`: `.npmrc`, git's user config), every user systemd
    unit but the rootless engine's own (`SYSTEMD_USER_DIR`), the docker CLI config rewritten to name only the
    rootless context (`DOCKER_CONFIG`: its credentials go), the shell rc files — replaced by `/etc/skel`'s copy
    (`.profile` with postinst's PATH floor) where there is one, else removed — and the user crontab;
  * on the engine — FIRST, and only the session user's own rootless dockerd (`session_user`), where no node
    service runs — every container, image, volume and network but those of a compose project the new org holds
    (the CP names those too; what a kept container uses stays), every buildx builder but `default` with its
    state, and the BuildKit cache. An old slot's stack still up fails the purge: its erase takes it down; the CP
    asks again. A rootful engine is never touched.
Accepted limits:
  * a node whose session uid is in the rootful `docker` group gave the old org root, so "nothing readable" there
    needs a reimage — and its engine is not purged at all;
  * the rootless engine's own unit (`docker.service`, its drop-ins) and CLI contexts stay, since the engine needs
    them: an old-org edit of them survives;
  * a process the old org left running outside docker (a user systemd unit already started, a nohup'd process) is
    not killed: the unit files and crontab go so nothing new starts, and the session uid's processes are the new
    org's too, so there is no uid-wide kill;
  * anything outside the named paths (files the old org wrote elsewhere in the home or in /var/tmp) stays."""
from __future__ import annotations

import getpass
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import env_helpers
import host_platform
import session_user

log = logging.getLogger("jaahd.slot_residue")

#: The shared /tmp a vnode's daemon, runner and user manager all bind (`vnodehelper.layout.tmp_dir`).
TMP_ROOT = Path("/tmp")
#: The task-keyed staging roots (`fleet_dispatch.REPORT_TMP_ROOT`, `_ATTACHMENTS_ROOT`, `_TASK_INPUTS_ROOT`).
TASK_DIR_ROOTS = ("cockpit-task-reports", "cockpit-task-attachments", "cockpit-task-inputs")
#: A task dir or pytest run untouched this long is no live run's (a dispatch creates its dirs at launch).
STALE_SECONDS = 24 * 3600

_TASK_DIR = re.compile(r"task-([0-9]+)", re.ASCII)
_TASK_TMP = re.compile(r"task-([0-9]+)-[A-Za-z0-9._-]+", re.ASCII)
_SLOT_STACK = re.compile(r"p([0-9]+)(?:-([0-9]+))?", re.ASCII)

# `break_cause.munge` — PATH-LOADED, as in `session_sweep`: `hooks/` has no `__init__.py`.
_BREAK_CAUSE_SPEC = importlib.util.spec_from_file_location(
    "break_cause", Path(__file__).resolve().parent / "hooks" / "break_cause.py"
)
_break_cause = importlib.util.module_from_spec(_BREAK_CAUSE_SPEC)
_BREAK_CAUSE_SPEC.loader.exec_module(_break_cause)


def remove_tree(path: str) -> str:
    """`rm -rf` one path as the session user where the uid split applies, else in-process. "" or why not."""
    if session_user.switch_needed():
        return session_user.remove_tree_as_session_user(path)
    try:
        shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return str(exc)
    return ""


def _claims(name: str, munged: str) -> bool:
    """Is scratch dir `name` the munged cwd `munged`, or a subdirectory of it? A `-` must follow, so slot 1
    never claims slot 10's scratch."""
    return name == munged or name.startswith(munged + "-")


# `find -printf "%y %T@ %f\0"` for macOS, whose BSD `find` has no `-printf` (every delegated listing there
# failed, so the sweep silently removed nothing): the bundle's interpreter, which the session user can run,
# lists with `scandir`/lstat and prints the same records. Kinds other than `d`/`l` read as `f` either way.
# An entry gone mid-listing is skipped, as GNU `find` warns and carries on.
_LIST_SCRIPT = ("import os,sys\n"
                "for e in os.scandir(sys.argv[1]):\n"
                "    try:\n"
                "        k = 'l' if e.is_symlink() else 'd' if e.is_dir(follow_symlinks=False) else 'f'\n"
                "        m = e.stat(follow_symlinks=False).st_mtime\n"
                "    except FileNotFoundError:\n"
                "        continue\n"
                "    os.write(1, ('%s %r ' % (k, m)).encode() + os.fsencode(e.name) + b'\\0')\n")


def _list_argv(directory: Path) -> list[str]:
    """The delegated listing's argv: GNU `find -printf` on Linux, the bundle Python on macOS (`_LIST_SCRIPT`)."""
    if host_platform.is_macos():
        return [host_platform.paths().python, "-I", "-c", _LIST_SCRIPT, str(directory)]
    return ["find", str(directory), "-mindepth", "1", "-maxdepth", "1", "-printf", "%y %T@ %f\\0"]


def _entries(directory: Path) -> Optional[list[tuple[str, str, float]]]:
    """`(name, kind, mtime)` for every entry directly in `directory` — kind `d`/`l`/`f` from an lstat, so a
    link is never a directory — or None when it cannot be listed. Listed AS THE SESSION USER where the uid
    split applies: these directories are its own, mode 0700."""
    if session_user.switch_needed():
        try:
            proc = session_user.run_as_session_user(_list_argv(directory))
        except Exception as exc:  # noqa: BLE001 — a delegation failure is an unreadable listing
            log.warning("slot_residue: cannot list %s (%s)", directory, exc)
            return None
        if proc.returncode != 0:
            return None
        out = []
        for record in proc.stdout.split("\0"):
            kind, _, rest = record.partition(" ")
            stamp, _, name = rest.partition(" ")
            if name:
                out.append((name, "d" if kind == "d" else "l" if kind == "l" else "f", float(stamp)))
        return out
    try:
        with os.scandir(directory) as it:
            return [(e.name, "l" if e.is_symlink() else "d" if e.is_dir(follow_symlinks=False) else "f",
                     e.stat(follow_symlinks=False).st_mtime) for e in it]
    except OSError:
        return None


def _scratch_entries(tmp_root: Optional[Path]) -> list[Path]:
    """Every directory directly under a `/tmp/claude-*/` (Claude Code's scratch roots, one per uid)."""
    tmp_root = tmp_root or TMP_ROOT
    out: list[Path] = []
    for root, kind, _mtime in _entries(tmp_root) or []:
        if root.startswith("claude-") and kind == "d":
            out.extend(tmp_root / root / name for name, k, _m in _entries(tmp_root / root) or [] if k == "d")
    return out


def scratch_dirs_for(slot_path: str, tmp_root: Optional[Path] = None) -> list[str]:
    """The Claude Code scratch dirs of sessions run in `slot_path` or below it."""
    munged = _break_cause.project_name(slot_path)
    return [str(p) for p in _scratch_entries(tmp_root) if _claims(p.name, munged)]


def purge_scratch(slot_path: str, tmp_root: Optional[Path] = None) -> list[str]:
    """Remove `slot_path`'s scratch dirs (the erase's step). Returns the paths removed."""
    if host_platform.is_windows():
        return []
    removed: list[str] = []
    for path in scratch_dirs_for(slot_path, tmp_root):
        err = remove_tree(path)
        if err:
            log.warning("slot_residue: could not remove %s: %s", path, err)
        else:
            removed.append(path)
    return removed


# ── The sweep ────────────────────────────────────────────────────────────────────────────────────────

def _slot_dirs(home: str) -> Optional[list[str]]:
    """Every directory two levels under `<home>/repos/` (slots, `main`, `_meta`, `_repos`: conservative —
    each one protects its scratch). None when `repos/` cannot be listed."""
    repos = Path(home) / "repos"
    try:
        projects = [p for p in repos.iterdir() if p.is_dir()]
        return [str(s) for p in projects for s in p.iterdir() if s.is_dir()]
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("slot_residue: cannot list %s (%s) — no scratch swept this pass", repos, exc)
        return None


def orphan_scratch(home: str, tmp_root: Optional[Path] = None) -> Optional[list[str]]:
    """Scratch dirs of a cwd at or below a slot path `<home>/repos/<project>/<slot>` that no existing slot
    directory claims. The scratch is listed BEFORE the slots, so a slot made in between protects its fresh
    scratch. None = no slot listing."""
    shape = re.compile(re.escape(_break_cause.project_name(os.path.join(home, "repos"))) + r"-[0-9]+-.+")
    entries = [p for p in _scratch_entries(tmp_root) if shape.fullmatch(p.name)]
    slot_dirs = _slot_dirs(home)
    if slot_dirs is None:
        return None
    live = [_break_cause.project_name(d) for d in slot_dirs]
    return [str(p) for p in entries if not any(_claims(p.name, m) for m in live)]


def orphan_task_dirs(held: set[str], now: float, tmp_root: Optional[Path] = None) -> list[str]:
    """`task-<id>` staging dirs no slot or live runner holds, untouched for `STALE_SECONDS`."""
    tmp_root = tmp_root or TMP_ROOT
    out: list[str] = []
    for root in TASK_DIR_ROOTS:
        for name, _kind, mtime in _entries(tmp_root / root) or []:
            match = _TASK_DIR.fullmatch(name)
            if match and match.group(1) not in held and now - mtime > STALE_SECONDS:
                out.append(str(tmp_root / root / name))
    return out


def orphan_task_tmp(held: set[str], now: float, tmp_root: Optional[Path] = None) -> list[str]:
    """`/tmp/task-<id>-<name>` entries the session user owns, no slot or live runner holds, untouched for
    `STALE_SECONDS`: a runner's screenshots and gate logs, which no erase names."""
    tmp_root = tmp_root or TMP_ROOT
    uid = session_user.session_uid()
    uid = os.getuid() if uid is None else uid
    out: list[str] = []
    for name, _kind, mtime in _entries(tmp_root) or []:
        match = _TASK_TMP.fullmatch(name)
        if not match or match.group(1) in held or now - mtime <= STALE_SECONDS:
            continue
        try:
            owner = os.lstat(tmp_root / name).st_uid
        except OSError:
            continue
        if owner == uid:
            out.append(str(tmp_root / name))
    return out


def stale_pytest_runs(now: float, tmp_root: Optional[Path] = None) -> list[str]:
    """Entries of the session user's `/tmp/pytest-of-<user>/` untouched for `STALE_SECONDS` (pytest keeps its
    newest three runs and leaves `garbage-*` behind when a removal fails; neither is a live run's after a day)."""
    root = (tmp_root or TMP_ROOT) / f"pytest-of-{session_user.session_user() or getpass.getuser()}"
    return [str(root / name) for name, _kind, mtime in _entries(root) or [] if now - mtime > STALE_SECONDS]


def _held_tasks(node_id: str, api) -> Optional[set[str]]:
    """Task ids a slot row on this node or a live runner holds. None when the pool cannot be read."""
    import runner_registry  # noqa: PLC0415 — deferred: only the sweep reads it

    records = api.agent_api_pool_worktrees(node_id)
    if records is None:
        return None
    held = {str(r.get("task_id")) for r in records if r.get("task_id") is not None}
    return held | {r.task_id for r in runner_registry.live_runners()}


def _docker_lines(argv: list[str], cwd: str) -> Optional[list[str]]:
    import slot_destroy  # noqa: PLC0415 — deferred: slot_destroy → commands is a cycle at import

    reply = slot_destroy._shell(argv, cwd)
    if reply.get("exit_code") != 0:
        return None
    return [ln.strip() for ln in (reply.get("stdout") or "").splitlines() if ln.strip()]


def orphan_stacks(home: str, live: Optional[frozenset[str]], listed: Iterable[str]) -> list[str]:
    """Compose projects of the slot-stack shape whose slot is gone: no `.env` names them and no slot
    directory they would belong to exists. Nothing when `live` is None."""
    if live is None:
        return []
    out: list[str] = []
    for name in sorted(set(listed)):
        match = _SLOT_STACK.fullmatch(name)
        if not match or name in live:
            continue
        project, number = match.groups()
        if (Path(home) / "repos" / project / (number or "main")).exists():
            continue
        out.append(name)
    return out


def _listed_stacks(home: str) -> Optional[list[str]]:
    """Every compose project label on this node's engine (containers and volumes). None = could not ask."""
    label = '{{.Label "com.docker.compose.project"}}'
    containers = _docker_lines(["docker", "ps", "-a", "--format", label], home)
    volumes = _docker_lines(["docker", "volume", "ls", "--format", label], home)
    if containers is None or volumes is None:
        return None
    return containers + volumes


def sweep(
    *,
    home: Optional[str] = None,
    node_id: Optional[str] = None,
    tmp_root: Optional[Path] = None,
    now: Optional[float] = None,
    api=None,
    remove: Callable[[str], str] = remove_tree,
    stacks: Optional[Callable[[str], Optional[list[str]]]] = None,
    down: Optional[Callable[[str, str], tuple[bool, str]]] = None,
) -> dict[str, Any]:
    """One pass. Returns its account of itself (also the journal line). Every dependency is injectable."""
    import slot_destroy  # noqa: PLC0415 — deferred: the cycle `_docker_lines` names

    if api is None:
        import control_plane_api as api  # noqa: PLC0415
    stamp = time.time() if now is None else now
    result: dict[str, Any] = {"removed": [], "failed": [], "stacks_down": [], "skipped": []}
    if host_platform.is_windows():
        result["skipped"].append("windows")
        return result
    if sys.platform != "win32":  # `disk_reclaim` imports `pwd`: fenced for the platform import-smoke
        import disk_reclaim  # noqa: PLC0415 — deferred: heavy, and only the sweep needs `live_projects`
    if home is None:
        try:
            home = env_helpers.jaah_home()
        except Exception as exc:  # noqa: BLE001 — an unconfigured host is a skip, never a traceback
            log.warning("slot_residue: no JAAH_HOME (%s) — nothing swept", exc)
            result["skipped"].append("no_jaah_home")
            return result
    home = os.path.realpath(home)

    doomed: list[str] = []
    scratch = orphan_scratch(home, tmp_root)
    if scratch is None:
        result["skipped"].append("scratch")
    else:
        doomed.extend(scratch)

    node = node_id if node_id is not None else api.this_node_id()
    held = _held_tasks(node, api) if node else None
    if held is None:
        result["skipped"].append("task_dirs")
    else:
        doomed.extend(orphan_task_dirs(held, stamp, tmp_root))
        doomed.extend(orphan_task_tmp(held, stamp, tmp_root))
    doomed.extend(stale_pytest_runs(stamp, tmp_root))

    for path in doomed:
        err = remove(path)
        (result["failed"] if err else result["removed"]).append(path)
        if err:
            log.warning("slot_residue: could not remove %s: %s", path, err)

    listed = (stacks or _listed_stacks)(home)
    if listed is None:
        result["skipped"].append("stacks")
    else:
        for name in orphan_stacks(home, disk_reclaim.live_projects(home), listed):
            ok, detail = (down or slot_destroy.compose_down)(name, home)
            (result["stacks_down"] if ok else result["failed"]).append(name)
            if not ok:
                log.warning("slot_residue: %s", detail)

    log.info("slot_residue: removed=%d stacks_down=%d failed=%d skipped=%s", len(result["removed"]),
             len(result["stacks_down"]), len(result["failed"]), ",".join(result["skipped"]) or "-")
    return result


# ── The org-transfer purge ───────────────────────────────────────────────────────────────────────────

#: What the transfer purge removes under the session user's home, relative to it. The shared caches first (npm's
#: may hold private-package tarballs), then the credential and persistence dotfiles a runner writes — `.gitconfig`
#: and `.config/git` (where a `core.hooksPath` lives) are never read by daemon git (`git_env.NO_USER_CONFIG`), so
#: only the next session would run what they name.
TRANSFER_CACHE_DIRS = (".npm", ".cache/pip", ".cache/uv", ".cargo/registry", ".cargo/git", "go/pkg/mod",
                       ".cache/go-build")
#: Prefixes of a `.cache` entry the purge removes (Playwright keeps `ms-playwright`, `ms-playwright-go`, ...).
TRANSFER_CACHE_PREFIXES = ("ms-playwright",)
TRANSFER_DOTFILES = (".npmrc", ".gitconfig", ".config/git")
#: The docker CLI config: credentials (`auths`, `credHelpers`, `credsStore`) and `proxies` go; it is REWRITTEN to
#: name only the rootless engine's context, which the CLI needs to reach the session user's own dockerd.
DOCKER_CONFIG = ".docker/config.json"
_ROOTLESS_CONTEXT = "rootless"
#: User systemd units: every entry goes but the rootless engine's own (`node_reprovision.ROOTLESS_USER_UNIT`,
#: its drop-ins, and its `default.target.wants` link), without which the engine does not come back after a reboot.
SYSTEMD_USER_DIR = ".config/systemd/user"
_ENGINE_UNITS = frozenset({"docker.service", "docker.service.d"})
_WANTS_DIR = "default.target.wants"
#: The PATH floor the package's postinst (`converge_runner_dotfiles`) gives `.profile` where skel lacks it.
_PROFILE_PATH_FLOOR = ('\n# The runner CLI is installed per-user (packaging/postinst: converge_runner_cli).\n'
                       'if [ -d "$HOME/.local/bin" ] ; then\n    PATH="$HOME/.local/bin:$PATH"\nfi\n')
#: Shell rc files: reset to the skel copy where `/etc/skel` has one (Linux `~/.profile` puts `~/.local/bin`, the
#: CLI's home, on PATH — `clauded_wt.py`), else removed.
TRANSFER_RC_FILES = (".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc", ".zshenv",
                     ".zprofile", ".zlogin")
#: Go's module cache is read-only (0555 dirs): made writable before it is removed, or the removal fails.
_READ_ONLY_CACHE = "go/pkg/mod"
_COMPOSE_LABEL = "com.docker.compose.project"
_BUILTIN_NETWORKS = frozenset({"bridge", "host", "none"})


def _listed_or_absent(directory: Path) -> Optional[list[tuple[str, str, float]]]:
    """`_entries`, with a directory that is not there read as empty. None only when it IS there and unreadable."""
    entries = _entries(directory)
    if entries is None and not os.path.lexists(directory):
        return []
    return entries


def _home_paths(home: Path, unreadable: list[str]) -> list[str]:
    """The named paths (`TRANSFER_*`, `.cache/<TRANSFER_CACHE_PREFIXES>*`) that exist under `home`, found by listing
    each parent AS THE SESSION USER (`_entries`): under the uid split the daemon can neither list nor stat inside
    the 0700 home. Only a real directory is descended (a link named `.cache` leads nowhere); a home that is not
    there holds nothing, and a directory there but unlistable lands in `unreadable`."""
    listings: dict[Path, Optional[dict[str, str]]] = {}

    def names(directory: Path) -> Optional[dict[str, str]]:
        if directory not in listings:
            entries = _listed_or_absent(directory) if directory == home else _entries(directory)
            listings[directory] = None if entries is None else {n: k for n, k, _m in entries}
            if entries is None:
                unreadable.append(str(directory))
        return listings[directory]

    def present(rel: str) -> bool:
        *parents, leaf = Path(rel).parts
        directory = home
        for part in parents:
            if (names(directory) or {}).get(part) != "d":
                return False
            directory = directory / part
        return leaf in (names(directory) or {})

    out = [str(home / rel) for rel in (*TRANSFER_CACHE_DIRS, *TRANSFER_DOTFILES, DOCKER_CONFIG, *TRANSFER_RC_FILES)
           if present(rel)]
    if (names(home) or {}).get(".cache") == "d":
        out.extend(str(home / ".cache" / name) for name in sorted(names(home / ".cache") or {})
                   if name.startswith(TRANSFER_CACHE_PREFIXES))
    return out


def _kept_scratch(keep: set[str], jaah_home: str) -> list[str]:
    """The munged cwds of the slot directories the new org's stacks name — `p<project>` → `repos/<project>/main`,
    `p<project>-<n>` → `repos/<project>/<n>`, as `orphan_stacks` maps them: their scratch is the new org's."""
    out: list[str] = []
    for name in sorted(keep):
        match = _SLOT_STACK.fullmatch(name)
        if match:
            project, number = match.groups()
            out.append(_break_cause.project_name(os.path.join(jaah_home, "repos", project, number or "main")))
    return out


def transfer_paths(held: set[str], home_dir: str, tmp_root: Optional[Path] = None,
                   kept_scratch: Optional[list[str]] = None) -> tuple[list[str], list[str], list[str]]:
    """`(doomed, unreadable, unowned)`: the paths the transfer purge removes; the directories it could not list (each
    a failure — the purge must not report done over what it could not see); and the `claude-*` scratch roots the
    session user cannot list — another uid's, so unreadable to the new org's session too (a skip). `kept_scratch`:
    the munged cwds whose scratch stays (`_kept_scratch`); None = no scratch purged."""
    tmp_root = tmp_root or TMP_ROOT
    doomed: list[str] = []
    unreadable: list[str] = []
    unowned: list[str] = []
    for root in TASK_DIR_ROOTS:
        entries = _listed_or_absent(tmp_root / root)
        if entries is None:
            unreadable.append(str(tmp_root / root))
        for name, _kind, _mtime in entries or []:
            match = _TASK_DIR.fullmatch(name)
            if match and match.group(1) not in held:
                doomed.append(str(tmp_root / root / name))
    pytest_root = tmp_root / f"pytest-of-{session_user.session_user() or getpass.getuser()}"
    if os.path.lexists(pytest_root):
        doomed.append(str(pytest_root))
    if kept_scratch is not None:
        roots = _listed_or_absent(tmp_root)
        if roots is None:
            unreadable.append(str(tmp_root))
        for root, kind, _mtime in roots or []:
            if not root.startswith("claude-") or kind != "d":
                continue
            entries = _entries(tmp_root / root)
            if entries is None:
                unowned.append(str(tmp_root / root))
                continue
            doomed.extend(str(tmp_root / root / name) for name, k, _m in entries
                          if k == "d" and not any(_claims(name, munged) for munged in kept_scratch))
    if home_dir:
        doomed.extend(_home_paths(Path(home_dir), unreadable))
    return doomed, unreadable, unowned


_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
#: One transfer purge at a time: a CP that timed out and asked again must not race the run still going.
_PURGE_LOCK = threading.Lock()
#: A container's image, named volumes and networks, `/`-joined (none of the three can hold a `/` or a space).
_CONTAINER_FIELD = ("{{.Image}}/{{range .Mounts}}{{.Name}},{{end}}/"
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$k}},{{end}}")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """One command as the session user where the uid split applies, else in-process (`remove_tree`'s rule)."""
    if session_user.switch_needed():
        return session_user.run_as_session_user(argv)
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)


def _account(result: dict[str, Any], what: str, reply: dict) -> None:
    """File one engine removal under `removed` or, with the engine's reason, `failed`."""
    if reply.get("exit_code") == 0:
        result["removed"].append(what)
    else:
        result["failed"].append(f"{what}: {(reply.get('stderr') or '').strip()[:300]}")


def _inspected(shell: Callable[[list[str], str], dict], cwd: str, kind: str, ids: list[str], field: str, *,
               key: str = "{{.Id}}", labels: str = ".Config.Labels") -> Optional[dict[str, tuple[str, Optional[str]]]]:
    """`{key: (field, compose project label or None)}` for `docker <kind> inspect` of `ids`; `key` and `field` are
    templates that render no space. The label is read as JSON, so a label carrying a newline cannot forge a line;
    only the ids asked about are kept. None = failed."""
    if not ids:
        return {}
    reply = shell(["docker", kind, "inspect", "--format",
                   f'{key} {field} {{{{json (index {labels} "{_COMPOSE_LABEL}")}}}}', *ids], cwd)
    if reply.get("exit_code") != 0:
        return None
    wanted, out = set(ids), {}
    for line in (reply.get("stdout") or "").splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3 or parts[0] not in wanted or parts[0] in out:
            continue
        try:
            label = json.loads(parts[2])
        except ValueError:
            continue
        out[parts[0]] = (parts[1], label if isinstance(label, str) and label else None)
    return out


def _containers(shell: Callable[[list[str], str], dict],
                cwd: str) -> Optional[dict[str, tuple[str, set[str], set[str], Optional[str]]]]:
    """Every container on the engine, running or not: `{id: (image, volumes, networks, compose project or None)}`.
    One the inspect did not answer for reads as no project's. None = could not ask."""
    listed = shell(["docker", "ps", "-a", "-q", "--no-trunc"], cwd)
    if listed.get("exit_code") != 0:
        return None
    ids = sorted(set((listed.get("stdout") or "").split()))
    found = _inspected(shell, cwd, "container", ids, _CONTAINER_FIELD)
    if found is None:
        return None
    out = {}
    for ident in ids:
        detail, project = found.get(ident, ("//", None))
        image, _, rest = detail.partition("/")
        volumes, _, networks = rest.partition("/")
        out[ident] = (image, set(filter(None, volumes.split(","))), set(filter(None, networks.split(","))), project)
    return out


def _purge_containers(shell: Callable[[list[str], str], dict], cwd: str, keep: set[str],
                      result: dict[str, Any]) -> Optional[dict[str, tuple[str, set[str], set[str], Optional[str]]]]:
    """Every container but a compose project's the new org holds (`keep`), running or not — the engine is the session
    user's own rootless dockerd, so none is a node service's. One an old slot's stack still holds fails the purge
    (its erase takes the stack down; the CP asks again). Returns the containers left; None = could not ask."""
    found = _containers(shell, cwd)
    if found is None:
        result["failed"].append("docker containers: cannot be listed")
        return None
    for ident, (_image, _volumes, _networks, project) in found.items():
        if project in keep:
            continue
        if project and _SLOT_STACK.fullmatch(project):
            result["failed"].append(f"container {ident}: of {project}, not down yet")
            continue
        _account(result, f"container {ident}", shell(["docker", "container", "rm", "-f", ident], cwd))
    left = _containers(shell, cwd)
    if left is None:
        result["failed"].append("docker containers: cannot be listed")
    return left


def _purge_images(shell: Callable[[list[str], str], dict], cwd: str, keep: set[str],
                  containers: dict[str, tuple[str, set[str], set[str], Optional[str]]], result: dict[str, Any]) -> None:
    """Every image except those of a compose project the new org holds (`keep`) or one a container of it uses — a
    pulled image, a tagged build, or one whose label merely claims a project. One an old slot's stack still runs
    fails the purge (its erase takes the stack down; the CP asks again)."""
    images = shell(["docker", "image", "ls", "-q", "--no-trunc"], cwd)
    if images.get("exit_code") != 0:
        result["failed"].append("docker images: cannot be listed")
        return
    image_ids = sorted({i for i in (images.get("stdout") or "").split() if _IMAGE_ID.fullmatch(i)})
    labels = _inspected(shell, cwd, "image", image_ids, "{{.Id}}")
    if labels is None:
        result["failed"].append("docker images: cannot be inspected")
        return
    used_by: dict[str, list[Optional[str]]] = {}
    for image, _volumes, _networks, project in containers.values():
        used_by.setdefault(image, []).append(project)
    for image in image_ids:
        users = used_by.get(image, [])
        if (labels.get(image) or ("", None))[1] in keep or any(p in keep for p in users):
            continue
        stacks = sorted({p for p in users if p and _SLOT_STACK.fullmatch(p)})
        if stacks:
            result["failed"].append(f"image {image}: in use by {', '.join(stacks)}, not down yet")
            continue
        # Forced: an id with several tags is refused without it.
        _account(result, f"image {image}", shell(["docker", "image", "rm", "-f", image], cwd))


def _purge_kind(shell: Callable[[list[str], str], dict], cwd: str, keep: set[str], result: dict[str, Any],
                kind: str, listing: list[str], key: str, spare: set[str]) -> None:
    """Every volume or network but a compose project's the new org holds (`keep`) and those `spare` names (a kept
    container's, a builtin network); one an old slot's stack holds fails the purge, as its containers do."""
    listed = shell(listing, cwd)
    ids = sorted(set((listed.get("stdout") or "").split())) if listed.get("exit_code") == 0 else None
    found = None if ids is None else _inspected(shell, cwd, kind, ids, "{{.Name}}", key=key, labels=".Labels")
    if ids is None or found is None:
        result["failed"].append(f"docker {kind}s: cannot be listed")
        return
    for ident in ids:
        name, project = found.get(ident, (ident, None))
        if project in keep or name in spare:
            continue
        if project and _SLOT_STACK.fullmatch(project):
            result["failed"].append(f"{kind} {name}: of {project}, not down yet")
            continue
        # A volume is forced (no prompt); `network rm` has nothing to force.
        _account(result, f"{kind} {name}", shell(["docker", kind, "rm", *(["-f"] if kind == "volume" else []), ident],
                                                 cwd))


def _purge_builders(shell: Callable[[list[str], str], dict], cwd: str, result: dict[str, Any]) -> None:
    """Every buildx builder but `default`, with its BuildKit state (`buildx rm` drops it). A builder that is the
    engine's own (a docker context's) cannot be removed and holds no old-org state of its own: that refusal is a
    skip. No buildx plugin, nothing to remove."""
    listed = shell(["docker", "buildx", "ls", "--format", "{{.Name}}"], cwd)
    if listed.get("exit_code") != 0:
        result["skipped"].append("docker buildx: not installed")
        return
    for name in sorted({n.rstrip("*") for n in (listed.get("stdout") or "").split()} - {"default", ""}):
        reply = shell(["docker", "buildx", "rm", "-f", name], cwd)
        why = (reply.get("stderr") or "").strip()
        if reply.get("exit_code") != 0 and re.search(r"default|context", why, re.IGNORECASE):
            result["skipped"].append(f"builder {name}: {why[:200]}")
            continue
        _account(result, f"builder {name}", reply)


def _purge_engine(shell: Callable[[list[str], str], dict], cwd: str, keep: set[str], result: dict[str, Any],
                  installed: bool = False) -> None:
    """The engine's share (module docstring): containers first, so what they held is free; then images, volumes and
    networks — skipped when the containers could not be listed, since what a kept one uses is then unknown — then
    the buildx builders and the BuildKit cache. Only on the session user's ROOTLESS engine: a rootful one is shared
    with whatever else runs on the host, and a session uid that reaches it gave the old org root (a reimage).
    `installed`: the session user HAS a rootless engine (its unit is there), so one that does not answer is a
    failure — its store still holds the old org's — and the CP asks again."""
    info = shell(["docker", "info", "--format", "{{json .SecurityOptions}}"], cwd)
    if info.get("exit_code") != 0:
        (result["failed"] if installed else result["skipped"]).append("docker: engine not reachable")
        return
    if "rootless" not in (info.get("stdout") or ""):
        # The CLI's context (the old org could repoint it) reaches another engine while the user's own is there.
        (result["failed"] if installed else result["skipped"]).append(
            "docker: not the session user's rootless engine — nothing on it purged")
        return
    containers = _purge_containers(shell, cwd, keep, result)
    if containers is not None:
        _purge_images(shell, cwd, keep, containers, result)
        kept = [c for c in containers.values() if c[3] in keep]
        _purge_kind(shell, cwd, keep, result, "volume", ["docker", "volume", "ls", "-q"], "{{.Name}}",
                    set().union(*(c[1] for c in kept)))
        _purge_kind(shell, cwd, keep, result, "network", ["docker", "network", "ls", "-q", "--no-trunc"], "{{.Id}}",
                    set(_BUILTIN_NETWORKS).union(*(c[2] for c in kept)))
    _purge_builders(shell, cwd, result)
    _account(result, "buildkit cache", shell(["docker", "builder", "prune", "-a", "-f"], cwd))


def _purge_crontab(run: Callable[[list[str]], subprocess.CompletedProcess], result: dict[str, Any]) -> None:
    """The session user's crontab (`crontab -r`), so nothing the old org scheduled starts again. None there is
    done; no `crontab` binary (in-process, or rc 127 from the delegated shell) is a skip."""
    try:
        proc = run(["crontab", "-r"])
    except FileNotFoundError:
        result["skipped"].append("crontab: not installed")
        return
    except Exception as exc:  # noqa: BLE001 — a delegation failure is a failed step, never a traceback
        result["failed"].append(f"crontab: {exc}")
        return
    why = (proc.stderr or "").strip()
    if proc.returncode == 0:
        result["removed"].append("crontab")
    elif proc.returncode == 127:
        result["skipped"].append("crontab: not installed")
    elif "no crontab for" not in why:
        result["failed"].append(f"crontab: {why[:300]}")


def _reset_rc(path: str, skel_dir: Path, remove: Callable[[str], str]) -> str:
    """Rc file `path` REPLACED by its skel copy — `.profile` with postinst's PATH floor when the copy lacks it, as
    postinst writes it — or removed where skel has none. A rename over the name, so a planted link is
    replaced, never followed, and a failed write leaves the old file for the retry. "" or why not."""
    name = os.path.basename(path)
    try:
        content = (skel_dir / name).read_text()
    except FileNotFoundError:
        content = "" if name == ".profile" else None
    except (OSError, UnicodeDecodeError) as exc:
        return f"{skel_dir}: {exc}"
    if name == ".profile" and ".local/bin" not in content:
        content += _PROFILE_PATH_FLOOR
    if content is None:
        return remove(path)
    return session_user.replace_file_as_session_user(path, content, mode=0o644)


def _reset_docker_config(path: str, run: Callable[[list[str]], subprocess.CompletedProcess]) -> str:
    """`DOCKER_CONFIG` rewritten to name only the rootless context, and only when it named it (any other context the
    old org could have defined itself): `{}` otherwise. Read as the session user (`run`). "" or why not."""
    try:
        proc = run(["cat", "--", path])
    except Exception as exc:  # noqa: BLE001 — a delegation failure is a failed step, never a traceback
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or f"cat rc {proc.returncode}").strip()[:300]
    try:
        current = json.loads(proc.stdout or "{}").get("currentContext")
    except (ValueError, AttributeError):
        current = None
    content = {"currentContext": _ROOTLESS_CONTEXT} if current == _ROOTLESS_CONTEXT else {}
    return session_user.replace_file_as_session_user(path, json.dumps(content) + "\n", mode=0o600)


def _systemd_user_entries(home: Path, unreadable: list[str]) -> list[str]:
    """Every user unit, drop-in and enablement link under `SYSTEMD_USER_DIR` but the rootless engine's own."""
    root = home / SYSTEMD_USER_DIR
    entries = _listed_or_absent(root)
    if entries is None:
        unreadable.append(str(root))
    out: list[str] = []
    for name, kind, _mtime in entries or []:
        if name in _ENGINE_UNITS:
            continue
        if name == _WANTS_DIR and kind == "d":
            wants = _entries(root / name)
            if wants is None:
                unreadable.append(str(root / name))
            out.extend(str(root / name / w) for w, _k, _m in wants or [] if w not in _ENGINE_UNITS)
            continue
        out.append(str(root / name))
    return out


def transfer_purge(
    held: set[str],
    keep: set[str],
    *,
    home_dir: Optional[str] = None,
    jaah_home: Optional[str] = None,
    tmp_root: Optional[Path] = None,
    skel_dir: Path = Path("/etc/skel"),
    remove: Callable[[str], str] = remove_tree,
    shell: Optional[Callable[[list[str], str], dict]] = None,
    run: Callable[[list[str]], subprocess.CompletedProcess] = _run,
) -> dict[str, Any]:
    """Remove the named residue the old org left readable to the new one (module docstring). `held`: the task ids,
    `keep`: the compose projects, the NEW org holds on this node — the CP's lists, never this node's own registry,
    which still knows old-org runners. Returns `{removed, failed, skipped}`; anything in `failed` means the purge
    is not done."""
    result: dict[str, Any] = {"removed": [], "failed": [], "skipped": []}
    if host_platform.is_windows():
        result["skipped"].append("windows")
        return result
    if not _PURGE_LOCK.acquire(blocking=False):
        result["failed"].append("a transfer purge is already running on this node")
        return result
    try:
        if home_dir is None:
            home_dir = session_user.session_home() or ("" if session_user.switch_needed() else os.path.expanduser("~"))
            if not home_dir:
                result["failed"].append("the session user's home cannot be resolved")
        if jaah_home is None:
            try:
                jaah_home = env_helpers.jaah_home()
            except Exception as exc:  # noqa: BLE001 — an unconfigured host is a failed step, never a traceback
                result["failed"].append(f"the jaah home cannot be resolved ({exc}): no scratch purged")
        # The engine FIRST: it is reached through the CLI config the dotfile step below rewrites.
        if shell is None and shutil.which("docker") is None:
            result["skipped"].append("docker: not installed")
        else:
            if shell is None:
                import slot_destroy  # noqa: PLC0415 — deferred: the cycle `_docker_lines` names

                shell = slot_destroy._shell
            units = _entries(Path(home_dir) / SYSTEMD_USER_DIR) if home_dir else None
            _purge_engine(shell, home_dir or str(tmp_root or TMP_ROOT), keep, result,
                          installed=any(name == "docker.service" for name, _k, _m in units or []))
        kept_scratch = None if jaah_home is None else _kept_scratch(keep, os.path.realpath(jaah_home))
        doomed, unreadable, unowned = transfer_paths(held, home_dir, tmp_root, kept_scratch)
        if home_dir:
            doomed.extend(_systemd_user_entries(Path(home_dir), unreadable))
        result["failed"].extend(f"{path}: cannot be listed" for path in unreadable)
        result["skipped"].extend(f"{path}: another uid's scratch" for path in unowned)
        rc_files = {os.path.join(home_dir, name) for name in TRANSFER_RC_FILES} if home_dir else set()
        for path in doomed:
            if home_dir and path == os.path.join(home_dir, _READ_ONLY_CACHE):
                try:
                    run(["chmod", "-R", "u+w", path])
                except Exception:  # noqa: BLE001 — the removal that follows reports what is left
                    pass
            if path in rc_files:
                err = _reset_rc(path, skel_dir, remove)
            elif home_dir and path == os.path.join(home_dir, DOCKER_CONFIG):
                err = _reset_docker_config(path, run)
            else:
                err = remove(path)
            if err:
                result["failed"].append(f"{path}: {err}")
            else:
                result["removed"].append(path)
        _purge_crontab(run, result)
    finally:
        _PURGE_LOCK.release()
    log.info("slot_residue: transfer purge removed=%d failed=%d skipped=%s", len(result["removed"]),
             len(result["failed"]), ",".join(result["skipped"]) or "-")
    return result


__all__ = ["purge_scratch", "scratch_dirs_for", "sweep", "transfer_purge"]
