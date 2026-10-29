#!/usr/bin/env python3
"""The node's DISK RECLAIM: bounded CI Docker stores and lean runner trees on every rootless engine of
this host, run as each engine's own owner, with no control plane, SSM, tag or LLM in the path.

Three entry points, one rule:

    disk_reclaim.py --pass                 # ROOT, by jaah-reclaim.service (marker → jaah-reclaim.path)
    disk_reclaim.py --engine-self [...]    # AS AN ENGINE'S OWNER: the per-engine rows, one JSON line out
    disk_reclaim.py --tree-self <tree>     # AS A RUNNER TREE'S OWNER: the runner-tree rows, one JSON line out

The per-job hook (`/usr/libexec/jaah-reclaim-job.sh`, wired into every PERSISTENT runner's `.env` by
the GHA runner installer) is `--engine-self` run by the runner user against its own engine.
The root pass never touches an engine or a runner tree as root: it spawns those two modes under the owner's
uid (`setpriv`), so a symlink a workflow planted can only ever reach that workflow's own user.

THE SELECTION RULE (`select_engine_removals`), per engine — never below the cap at any disk level:

  | Object                           | Runner engine         | Session engine                            |
  | Running container                | keep                  | keep                                      |
  | Stopped container                | remove                | remove unless its compose project is live |
  | Image used by a kept container   | keep                  | keep                                      |
  | Locally built image              | remove                | remove unless named/labelled for a live   |
  | Dangling image                   | remove                | remove                                    |
  | Pulled image                     | keep while Images ≤ cap; above, remove UNUSED pulled oldest-created  |
  |                                  |                       |   first, re-measuring after each; never a |
  |                                  |                       |   live slot's                             |
  | Volume referenced at pass start  | keep                  | keep                                      |
  | Volume unreferenced at start     | remove                | remove unless a live slot project's       |
  | BuildKit cache                   | prune to max(cap − images, 1 GB); cap = engine_cap_bytes (≥ ENGINE_CACHE_CAP_GB) |

References are taken from ONE snapshot at the start, before any container is removed, so a volume held only
by a stopped container survives the pass that removes the container (and goes on the next). "Live slot
projects" are the COMPOSE_PROJECT_NAME of every worktree `.env` on the node; when that enumeration fails the
session engines it governs remove no volume, no stopped container and no image but a dangling one (absent
denies). "Locally built" is `Image.locally_built` — on the containerd store a local build has a digest too.
Images go by tag, never forced, so the engine refuses one any container uses; a `created` container is kept for
`CREATED_GRACE_S` (a `docker create` about to start), then goes as a stopped one (a `docker run` that never began).

A CO-LOCATED NODE'S engines (any uid in a vnode's span) always get the base cap (`engine_cap_for`): the host's
free terabytes are not the node's to fill, its own disk cap is (the CP's vnode record).

A co-located node's `/tmp` and `/var/tmp` (`layout.tmp_dir`/`var_tmp_dir`, bound dirs nothing else ages) lose
every top-level directory of its session user in which nothing changed for `TMP_STALE_DAYS`
(`sweep_stale_tmp`, run as that user).

Runner trees (`plan_tree_cleanup`), only when the runner is IDLE: every `bin.*`/`externals.*` that is not
the live version goes, `_update/` goes, `_work/_temp/` is emptied; `_work/_tool`, `_work/<repo>` and `_diag`
are never touched. Ephemeral host slots (one user per slot, wiped per job by `gha_job.py`) are never
enumerated. A busy runner — its uid has a `Runner.Worker` process — is skipped whole; undeterminable is busy.

The report (`build_report`) is the pinned `POST /api/agent/disk-reclaims` body; the pass writes it to the
spool (`disk_events.write_report`) and jaahd uploads it (`disk_events.drain_spool`).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
try:
    import pwd
except ImportError:  # Windows: the bundle imports this module (`slot_repos`' erase); every reclaim path is Linux-only.
    pwd = None
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

# `apps/jaahd` on sys.path, APPENDED (never inserted at 0), for `main.py`'s reason.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.append(str(_HERE))

import disk_events  # noqa: E402 — stdlib-only: the report spool, its writer, the warn line, `tree_bytes`
import disk_reclaim_trigger  # noqa: E402 — the marker's one home
import cloud_modules  # noqa: E402 — stdlib-only: the cloud-only modules, by name
import env_helpers  # noqa: E402 — stdlib-only: the one COMPOSE_PROJECT_NAME rule
from host_platform.linux import JAAH_PYTHON  # noqa: E402 — stdlib-only: the node's own interpreter
from vnodehelper import layout  # noqa: E402 — stdlib-only: every vnode name, uid and path

# `install.py` BY PATH (`gha_install_loader`): the runner user names, the persistent runners root and the
# pinned tarball are single-homed there. None on a community-edition node, which runs no GHA pool and no root pass:
# there only the daemon's own readers (`live_projects`) are reached.
_gha_install_loader = cloud_modules.load("gha_install_loader")
install: Any = _gha_install_loader.load() if _gha_install_loader is not None else None

# ── the one cap ──────────────────────────────────────────────────────────────────────────────────────
# THE per-engine budget for Images + BuildKit cache, in GB (10^9 bytes, as `docker system df` counts).
# Base layers stay below it; above it the oldest UNUSED pulled images go first, then BuildKit is pruned to
# what the images leave.
# This is the FLOOR, and the whole cap under disk pressure, on a small disk (18–57 GB cloud nodes: 6 GB is
# right there), or whenever health is not positively shown (no readable warn line). While the disk is
# POSITIVELY healthy `engine_cap_bytes` scales it with the filesystem: a 1.8 TB host at 6–12 % use otherwise
# threw away 0.4–1 TB a week of cache the next gate rebuilt. The scaled cap reaches engines through the root
# pass; the per-job hook on a persistent runner cannot read the 0770 root:jaahd warn file, so it keeps the
# base cap unless it can read a warn value.
ENGINE_CACHE_CAP_GB = 6
GB = 1_000_000_000
# Per-engine share of the filesystem while healthy. 14 engines × 1 % ≈ 14 % worst case on a big host, well
# below the 75 % warn line — and crossing the warn line restores the tight cap on the next pass.
ENGINE_CACHE_CAP_DISK_FRACTION = 0.01
# BuildKit always keeps at least this much, however much of the cap images take: a cold cache re-pulls and
# re-builds every layer of the next job.
BUILD_CACHE_FLOOR_BYTES = 1 * GB


def engine_cap_bytes(total_bytes: int, used_pct: float, warn_pct: float | None) -> int:
    """THE per-engine cap in bytes. Pure. `max(base, fraction × filesystem)` only while health is positively
    shown (a known warn line, usage below it); exactly the base cap at/above the warn line, when `warn_pct`
    is None (unknown: an offline/older control plane, or a caller that cannot read the warn file) or when
    the filesystem's size is unknown."""
    base = ENGINE_CACHE_CAP_GB * GB
    if total_bytes <= 0 or warn_pct is None or used_pct >= warn_pct:
        return base
    return max(base, int(total_bytes * ENGINE_CACHE_CAP_DISK_FRACTION))


def engine_cap_for(engine: "Engine", vnodes: Iterable[layout.Vnode], host_cap: int) -> int:
    """The cap ENGINE gets from a pass whose host figure is `host_cap`: the base cap for a co-located node's
    engine, whose share is its own disk cap, not 1 % of the host's filesystem per engine (four engines on a
    1.8 TB host would otherwise keep ~72 GB of a 100 GB node before the host's warn line ever moves)."""
    if any(engine.uid in layout.uid_span(v) for v in vnodes):
        return ENGINE_CACHE_CAP_GB * GB
    return host_cap


def host_engine_cap_bytes(statvfs: Callable | None = None, warn_pct_file: Path | None = None) -> int:
    """`engine_cap_bytes` for this host's root filesystem; any failure reading it, or an unreadable warn file
    (the per-job hook's runner user cannot read it), is the base cap."""
    try:
        statvfs = statvfs or os.statvfs
        st = statvfs("/")
        used = disk_events.used_pct("/", statvfs)
        warn = disk_events.read_warn_pct(warn_pct_file) if warn_pct_file else disk_events.read_warn_pct()
        return engine_cap_bytes(st.f_blocks * st.f_frsize, used, warn)
    except Exception:  # noqa: BLE001 — the cap is advisory scale-up; never fail a reclaim over it
        return ENGINE_CACHE_CAP_GB * GB


# ── paths ────────────────────────────────────────────────────────────────────────────────────────────
RUN_USER_ROOT = Path("/run/user")
ROOTFUL_SOCKET = Path("/run/docker.sock")
PROC = Path("/proc")
SETPRIV = "/usr/bin/setpriv"
PYTHON = JAAH_PYTHON  # absolute: re-exec'd under setpriv as another uid, never a PATH lookup

# Per-call bounds. A prune walks every layer, so it gets the long one; everything else answers in seconds.
API_TIMEOUT_S = 120.0
PRUNE_TIMEOUT_S = 900.0
# Sizing every VOLUME (`/system/df?type=volume`) walks every file of every volume — measured past 280 s on a
# developer engine holding a few dozen ./dc databases. So it is asked once, at the end, under this bound, and
# a slow answer reports no figure rather than stalling the pass; the bytes a pass frees from volumes are
# measured on the removed volumes alone (`volume_bytes`).
VOLUME_DF_TIMEOUT_S = 60.0
# The cheap half of `docker system df`: images, BuildKit and containers (the `type` filter, API 1.42+; an older
# engine ignores it and answers everything).
_DF_FAST = "/system/df?type=image&type=build-cache&type=container"
CHILD_TIMEOUT_S = 1500.0
CONFIG_VERSION_TIMEOUT_S = 30.0

CLASS_RUNNER = "runner"
CLASS_SESSION = "session"
TRIGGERS = ("scheduled", "warn", "converge")
# The CLOSED key set of the report's `freed_bytes` (pinned contract §3.1), and the subset this module owns.
FREED_KEYS = ("ci_images", "build_cache", "stopped_containers", "orphan_volumes", "runner_versions",
              "runner_temp", "plugin_dedup", "orphan_users", "stale_tmp")
OWN_FREED_KEYS = ("ci_images", "build_cache", "stopped_containers", "orphan_volumes", "runner_versions",
                  "runner_temp")
# Sent only when non-zero: a control plane older than the key 422s the whole report, and a node may upgrade first.
SPARSE_FREED_KEYS = ("stale_tmp",)
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
# Everything but a container that has finished: a `created` one is a job's `docker create` about to start —
# for `CREATED_GRACE_S`; older, it is a `docker run` that never started, and goes as a stopped one.
_RUNNING_STATES = frozenset({"running", "paused", "restarting", "removing", "created"})
CREATED_GRACE_S = 3600
# Tags a CI build gives the images it makes (the deploy workflow's per-commit finals).
LOCAL_BUILD_PREFIXES = ("jaah-build/",)
# At most this many pulled images are evicted per engine per pass: each is followed by a re-measure.
MAX_EVICTIONS = 50


# ── the snapshot (what one engine holds) ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Container:
    id: str
    running: bool
    image_id: str
    volumes: tuple[str, ...] = ()
    compose_project: str = ""
    size_rw: int = 0


@dataclass(frozen=True)
class Image:
    id: str
    repo_tags: tuple[str, ...] = ()
    repo_digests: tuple[str, ...] = ()
    created: int = 0
    # Bytes removing it frees: its size minus what it shares with other images.
    unique_size: int = 0
    labels: dict = field(default_factory=dict)

    @property
    def dangling(self) -> bool:
        return not [t for t in self.repo_tags if t and t != "<none>:<none>"]

    @property
    def locally_built(self) -> bool:
        """Built on this engine, not pulled. NOT just "no RepoDigests": the containerd image store (Docker
        29's default) gives a local build a digest too (`p1-7-api@sha256:…`), so a compose label or a CI
        build tag also marks one."""
        if not [d for d in self.repo_digests if d and d != "<none>@<none>"]:
            return True
        if self.labels.get(COMPOSE_PROJECT_LABEL):
            return True
        return any(t.startswith(LOCAL_BUILD_PREFIXES) for t in self.repo_tags)


@dataclass(frozen=True)
class Volume:
    name: str
    labels: dict = field(default_factory=dict)
    mountpoint: str = ""


@dataclass(frozen=True)
class Snapshot:
    containers: tuple[Container, ...]
    images: tuple[Image, ...]
    volumes: tuple[Volume, ...]
    # Images on disk (`docker system df` LayersSize) and BuildKit cache (non-shared records).
    images_bytes: int
    build_cache_bytes: int


@dataclass(frozen=True)
class Plan:
    containers: tuple[str, ...]
    # Removed whatever the size: dangling and (per the engine class) locally built images.
    images: tuple[Image, ...]
    # Unused pulled images, OLDEST first — removed one at a time, re-measuring, only while above the cap
    # (`evict_until_cap`). Removal frees only the layers nobody else holds, which no snapshot says up front.
    evict: tuple[Image, ...]
    volumes: tuple[str, ...]


def _belongs_to_live(name: str, live: frozenset[str]) -> bool:
    """A compose name (`<project>`, `<project>-<svc>`, `<project>_<svc>`, a repo before `:`) of a live slot."""
    repo = name.split("@", 1)[0]
    if ":" in repo.rsplit("/", 1)[-1]:
        repo = repo.rsplit(":", 1)[0]
    return any(repo == p or repo.startswith(p + "-") or repo.startswith(p + "_") for p in live if p)


def select_engine_removals(snap: Snapshot, engine_class: str, live: frozenset[str] | None,
                           cap_bytes: int = ENGINE_CACHE_CAP_GB * GB) -> Plan:
    """THE RULE (module docstring table) over one engine's snapshot. Pure.

    `live` — the live slot projects that govern this engine, or None when they could not be enumerated:
    then a SESSION engine removes no stopped container, no locally built image and no volume."""
    runner = engine_class == CLASS_RUNNER
    unknown = live is None
    live_set = live or frozenset()

    def session_keeps(project: str = "", names: Iterable[str] = (), labels: dict | None = None) -> bool:
        if runner:
            return False
        if unknown:
            return True
        labelled = (labels or {}).get(COMPOSE_PROJECT_LABEL, "")
        if (project and project in live_set) or (labelled and labelled in live_set):
            return True
        return any(_belongs_to_live(n, live_set) for n in names)

    # Volume references come from the WHOLE snapshot, before any container goes.
    referenced = {v for c in snap.containers for v in c.volumes}

    gone_containers = [c for c in snap.containers
                       if not c.running and not session_keeps(project=c.compose_project)]
    gone_ids = {c.id for c in gone_containers}
    kept = [c for c in snap.containers if c.id not in gone_ids]
    used_images = {c.image_id for c in kept}

    remove_images: list[Image] = []
    pulled_unused: list[Image] = []
    for img in snap.images:
        if img.id in used_images:
            continue
        if img.dangling:
            remove_images.append(img)
        elif session_keeps(names=img.repo_tags, labels=img.labels):
            # A live slot's image (or any image while the live set is unknown) stays on a session engine,
            # pulled or built: its stack's next `./dc up` needs it.
            continue
        elif img.locally_built:
            remove_images.append(img)
        else:
            pulled_unused.append(img)

    gone_volumes = [v for v in snap.volumes
                    if v.name not in referenced
                    and not session_keeps(project=v.labels.get(COMPOSE_PROJECT_LABEL, ""), names=(v.name,))]

    return Plan(
        containers=tuple(c.id for c in gone_containers),
        images=tuple(remove_images),
        evict=tuple(sorted(pulled_unused, key=lambda i: (i.created, i.id))),
        volumes=tuple(v.name for v in gone_volumes),
    )


def evict_until_cap(candidates: Iterable[Image], measure: Callable[[], int], delete: Callable[[Image], bool],
                    cap_bytes: int = ENGINE_CACHE_CAP_GB * GB, max_evictions: int = MAX_EVICTIONS) -> list[str]:
    """Remove CANDIDATES in order while the MEASURED image bytes exceed the cap — never estimated, because
    images share layers and a snapshot cannot say what one removal frees. Returns the ids removed."""
    removed: list[str] = []
    for img in candidates:
        if len(removed) >= max_evictions or measure() <= cap_bytes:
            break
        if delete(img):
            removed.append(img.id)
    return removed


def build_cache_target(images_bytes: int, cap_bytes: int = ENGINE_CACHE_CAP_GB * GB) -> int:
    """What BuildKit may keep: the cap minus what the images take, never below `BUILD_CACHE_FLOOR_BYTES`."""
    return max(cap_bytes - images_bytes, BUILD_CACHE_FLOOR_BYTES)


# ── engine classification ────────────────────────────────────────────────────────────────────────────

def runner_user_names() -> frozenset[str]:
    """Every user name a runner engine can belong to, from the imported name functions — never a regex:
    the persistent host user, every ephemeral host slot account, every vnode's runner users."""
    names = {install.GHA_USER}
    names.update(install.slot_account(f"gha-{i}")[0] for i in range(1, 100))
    return frozenset(names)


def classify_owner(user: str, vnodes: Iterable[layout.Vnode] = ()) -> str:
    """`runner` when USER is a GHA runner user (host or vnode), else `session`."""
    if user in runner_user_names():
        return CLASS_RUNNER
    for v in vnodes:
        if any(user == layout.gha_user(v, n) for n in range(len(layout.GHA_OFFSETS))):
            return CLASS_RUNNER
    return CLASS_SESSION


@dataclass(frozen=True)
class Engine:
    socket: str
    uid: int
    gid: int
    user: str
    home: str
    engine_class: str


def enumerate_engines(vnodes: Iterable[layout.Vnode] = (), run_user_root: Path = RUN_USER_ROOT,
                      rootful: Path = ROOTFUL_SOCKET,
                      getpwuid: Callable[[int], pwd.struct_passwd] | None = None) -> list[Engine]:
    """Every rootless engine (`/run/user/<uid>/docker.sock` that IS a socket) plus the rootful one."""
    getpwuid = getpwuid or pwd.getpwuid
    vnodes = tuple(vnodes)
    engines: list[Engine] = []
    try:
        entries = sorted(os.scandir(run_user_root), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        sock = Path(entry.path) / "docker.sock"
        try:
            if not stat.S_ISSOCK(os.lstat(sock).st_mode):
                continue
        except OSError:
            continue
        uid = int(entry.name)
        try:
            pw = getpwuid(uid)
        except KeyError:
            continue
        engines.append(Engine(str(sock), uid, pw.pw_gid, pw.pw_name, pw.pw_dir,
                              classify_owner(pw.pw_name, vnodes)))
    try:
        if stat.S_ISSOCK(os.lstat(rootful).st_mode):
            engines.append(Engine(str(rootful), 0, 0, "root", "/root", CLASS_SESSION))
    except OSError:
        pass
    return engines


# ── busy runners ─────────────────────────────────────────────────────────────────────────────────────

def busy_uids(proc: Path = PROC) -> frozenset[int] | None:
    """Every uid holding a `Runner.Worker` (`install.runner_workers`, the one scanner), None when unreadable."""
    workers = install.runner_workers(proc)
    return None if workers is None else frozenset(uid for _pid, uid in workers)


def is_busy(uid: int, busy: frozenset[int] | None) -> bool:
    """Undeterminable (None) is busy — a reclaim must never race a job it could not see."""
    return busy is None or uid in busy


def ancestors(pid: int, proc: Path = PROC) -> set[int]:
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            text = Path(proc, str(pid), "status").read_text(errors="replace")
            pid = int(next(line for line in text.splitlines() if line.startswith("PPid:")).split()[1])
        except (OSError, StopIteration, ValueError, IndexError):
            break
    return seen


def other_worker_busy(uid: int, own_pid: int, proc: Path = PROC) -> bool:
    """The per-job hook's busy test: a `Runner.Worker` of this uid that is NOT the one running this hook.
    A persistent host shares one engine across every slot of user `gha`; another slot's live job owns it."""
    workers = install.runner_workers(proc)
    if workers is None:
        return True
    mine = ancestors(own_pid, proc)
    return any(w_uid == uid and pid not in mine for pid, w_uid in workers)


# ── live slot projects ───────────────────────────────────────────────────────────────────────────────

# A worktree `.env` past this is not one the pool wrote; the read is bounded either way.
_ENV_READ_CAP = 65536
# `getattr`: Windows has neither flag, and only a Linux reclaim opens these (the bundle still imports this module).
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _read_env_nofollow(slot_fd: int) -> str:
    """A slot's `.env`, read as ROOT out of a tree its user owns: never through a link, never blocking on a
    FIFO, never past `_ENV_READ_CAP`, regular files only. "" when there is none; OSError when it is not a
    file the pool would have written (the caller then denies the whole tree)."""
    try:
        fd = os.open(".env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=slot_fd)
    except FileNotFoundError:
        return ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _ENV_READ_CAP:
            raise OSError("not a regular .env")
        return os.read(fd, _ENV_READ_CAP).decode(errors="replace")
    finally:
        os.close(fd)


def live_projects(jaah_home: str | os.PathLike | None) -> frozenset[str] | None:
    """The COMPOSE_PROJECT_NAME (last occurrence, `env_helpers.compose_project_name`) of every slot `.env`
    under `<jaah_home>/repos/*/*/`, falling back to `<project>-<n>` as `reconcile.context.cpn_of` does. None when
    the tree cannot be enumerated — absent denies.

    Root walks a tree its session user owns, so every step is by descriptor with `O_NOFOLLOW`: a planted
    link, FIFO or oversized `.env` makes the whole tree None (nothing of it is removed) rather than steering
    root into another directory or hanging the pass."""
    if not jaah_home:
        return None
    names: set[str] = set()
    fds: list[int] = []
    try:
        home_fd = os.open(str(jaah_home), _DIR_FLAGS)
        fds.append(home_fd)
        repos_fd = os.open("repos", _DIR_FLAGS, dir_fd=home_fd)
        fds.append(repos_fd)
        for project in sorted(os.listdir(repos_fd)):
            if not stat.S_ISDIR(os.stat(project, dir_fd=repos_fd, follow_symlinks=False).st_mode):
                if stat.S_ISLNK(os.stat(project, dir_fd=repos_fd, follow_symlinks=False).st_mode):
                    return None
                continue
            project_fd = os.open(project, _DIR_FLAGS, dir_fd=repos_fd)
            fds.append(project_fd)
            for slot in sorted(os.listdir(project_fd)):
                # Only the pool's own slot names (`worktree_mechanics.core.slot_path`): `_meta` and a multi-repo
                # project's `_repos` (`slot_repos`) are not slots.
                if slot != "main" and not slot.isdigit():
                    continue
                mode = os.stat(slot, dir_fd=project_fd, follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    return None
                if not stat.S_ISDIR(mode):
                    continue
                slot_fd = os.open(slot, _DIR_FLAGS, dir_fd=project_fd)
                try:
                    text = _read_env_nofollow(slot_fd)
                finally:
                    os.close(slot_fd)
                names.add(env_helpers.compose_project_name(text) or f"{project}-{slot}")
            os.close(fds.pop())
    except OSError:
        return None
    finally:
        for fd in fds:
            os.close(fd)
    return frozenset(names)


def vnode_records(store_path: str = layout.STORE_PATH) -> list[layout.Vnode]:
    """The vnodes this host holds (`vnodehelper/store.py`'s pinned shape), [] on a regular node."""
    try:
        raw = json.loads(Path(store_path).read_text())
    except FileNotFoundError:
        return []
    out = []
    for rec in raw.get("vnodes") or []:
        try:
            out.append(layout.Vnode(int(rec["slot"]), rec.get("tag")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def live_for_engine(engine: Engine, host_live: frozenset[str] | None,
                    vnode_live: dict[layout.Vnode, frozenset[str] | None]) -> frozenset[str] | None:
    """The live set governing ENGINE: its vnode's tree when its uid is in a vnode's span, else the host's."""
    for v, live in vnode_live.items():
        if engine.uid in layout.uid_span(v):
            return live
    return host_live


# ── runner trees ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunnerTree:
    path: str
    uid: int
    gid: int
    user: str
    home: str


def _uid_or_none(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def enumerate_runner_trees(vnodes: Iterable[layout.Vnode] = (), host_root: Path | None = None,
                           host_uid: int | None | str = "lookup") -> list[RunnerTree]:
    """Every PERSISTENT runner tree: `install.RUNNERS_ROOT/gha-*` on the host, and each vnode runner's
    `layout.gha_home(v, n)/gha-runners/gha-<n+1>`. Ephemeral host slots live in their own homes
    (`install.slot_account`) and are never listed.

    A tree is listed only when it is owned by the uid that SHOULD own it (`install.GHA_USER` on the host,
    `layout.gha_uid` in a vnode): a runner that symlinked its tree at another user's directory would
    otherwise have that user's files cleaned up as that user."""
    host_root = install.RUNNERS_ROOT if host_root is None else host_root
    host_uid = _uid_or_none(install.GHA_USER) if host_uid == "lookup" else host_uid
    candidates: list[tuple[Path, int | None]] = []
    try:
        candidates += [(p, host_uid) for p in sorted(host_root.iterdir())
                       if install.SLOT_NAME_PATTERN.match(p.name)]
    except OSError:
        pass
    for v in vnodes:
        for n in range(len(layout.GHA_OFFSETS)):
            candidates.append((Path(layout.gha_home(v, n)) / "gha-runners" / f"gha-{n + 1}",
                               layout.gha_uid(v, n)))
    trees = []
    for path, expected in candidates:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if expected is None or not stat.S_ISDIR(st.st_mode) or st.st_uid != expected:
            continue
        try:
            pw = pwd.getpwuid(st.st_uid)
        except KeyError:
            continue
        trees.append(RunnerTree(str(path), st.st_uid, pw.pw_gid, pw.pw_name, pw.pw_dir))
    return trees


@dataclass(frozen=True)
class TreePlan:
    remove: tuple[str, ...]          # names directly under the tree (version dirs, `_update`)
    empty: tuple[str, ...]           # directories under the tree whose CONTENTS go (`_work/_temp`)


def plan_tree_cleanup(entries: dict[str, str | None], config_version: str | None = None) -> TreePlan:
    """Pure. ENTRIES maps each top-level name of a runner tree to its symlink target (None for a real dir).

    The live version is the target of the `bin`/`externals` symlinks a self-update leaves; when `bin` is a
    real directory instead, it is the version `config.sh --version` reports (CONFIG_VERSION), and with
    neither nothing versioned is removed."""
    keep: set[str] = set()
    known = True
    for base in ("bin", "externals"):
        target = entries.get(base)
        if target is not None:
            keep.add(os.path.basename(target.rstrip("/")))
        elif config_version:
            keep.add(f"{base}.{config_version}")
        else:
            known = False
    remove = []
    if known:
        remove = sorted(name for name in entries
                        if (name.startswith("bin.") or name.startswith("externals.")) and name not in keep)
    if "_update" in entries and entries["_update"] is None:
        remove.append("_update")
    return TreePlan(remove=tuple(remove), empty=("_work/_temp",))


def tree_entries(tree: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for entry in os.scandir(tree):
        if entry.is_symlink():
            out[entry.name] = os.readlink(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            out[entry.name] = None
    return out


def _remove(path: str) -> None:
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)


def config_version(tree: Path, run: Callable = subprocess.run) -> str | None:
    """The runner's own answer (`./config.sh --version`), run as the tree's owner by `--tree-self`."""
    try:
        out = run([str(tree / "config.sh"), "--version"], cwd=str(tree), capture_output=True, text=True,
                  timeout=CONFIG_VERSION_TIMEOUT_S, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    version = (out or "").strip().splitlines()[-1:] or [""]
    v = version[0].strip()
    return v if v and all(part.isdigit() for part in v.split(".")) else None


# A self-update stages `_update/` and the new `bin.<ver>`/`externals.<ver>` while the runner is idle (no
# `Runner.Worker`), so "idle" does not cover it: a tree whose `_update/` is younger than this is mid-update and
# keeps every version, and a version directory younger than a day is never removed.
UPDATE_SETTLE_S = 3600.0
VERSION_MIN_AGE_S = 86400.0
# A `_work/_temp` entry younger than this may belong to a job that started after the busy check.
TEMP_MIN_AGE_S = 600.0


def _age(path: str, now: float) -> float:
    try:
        return now - os.lstat(path).st_mtime
    except OSError:
        return 0.0


def apply_tree_cleanup(tree: Path, run: Callable = subprocess.run, now: Callable[[], float] = time.time,
                       busy: Callable[[int], bool] | None = None) -> dict:
    """`--tree-self`: run AS THE TREE'S OWNER. Returns `{runner_versions, runner_temp, error}` bytes.

    Checks once more that no job runs under this uid before removing anything (`install.uid_busy`)."""
    result = {"runner_versions": 0, "runner_temp": 0, "error": None}
    if (busy or install.uid_busy)(os.getuid()):
        result["skipped"] = "busy"
        return result
    t = now()
    try:
        entries = tree_entries(tree)
        updating = "_update" in entries and _age(str(tree / "_update"), t) < UPDATE_SETTLE_S
        version = None if entries.get("bin") is not None else config_version(tree, run)
        plan = plan_tree_cleanup(entries, version)
        for name in () if updating else plan.remove:
            target = str(tree / name)
            if name != "_update" and _age(target, t) < VERSION_MIN_AGE_S:
                continue
            size = disk_events.tree_bytes(target)
            _remove(target)
            result["runner_versions"] += size
        for rel in plan.empty:
            temp = tree / rel
            if temp.is_symlink() or not temp.is_dir():
                continue
            for child in os.scandir(temp):
                if _age(child.path, t) < TEMP_MIN_AGE_S:
                    continue
                size = disk_events.tree_bytes(child.path)
                _remove(child.path)
                result["runner_temp"] += size
    except OSError as exc:
        result["error"] = f"runner tree cleanup failed: {exc.strerror or type(exc).__name__}"
    return result


# ── a co-located node's /tmp ─────────────────────────────────────────────────────────────────────────

# A top-level directory in which nothing changed for this long is a leftover (a killed gate's temp tree, a
# browser profile), not work in flight. "Changed" is max(mtime, ctime): an extract (`tar x`, `cp -p`) restores
# old mtimes, never ctime. Its walk is bounded: one too big or too slow to prove stale is kept. A tree only READ
# for that long is not seen, an accepted limit.
TMP_STALE_DAYS = 3
TMP_WALK_ENTRY_CAP = 200_000
TMP_WALK_SECONDS = 60.0
# Never swept: the daemon's staging roots (`fleet_dispatch`: a task's attachments, inputs and reports, kept while
# it waits on a human for days; `console`'s pastes), a task's own `task-<id>-*` and an agent CLI's per-uid scratch
# root `<cli>-<uid>` (both aged by `slot_residue` only once no slot or runner holds the task), and dot-entries.
TMP_KEEP_PREFIXES = ("cockpit-", "console-paste", "task-", ".")


@dataclass(frozen=True)
class TmpSweep:
    path: str
    uid: int
    gid: int
    home: str


def enumerate_tmp_sweeps(vnodes: Iterable[layout.Vnode] = ()) -> list[TmpSweep]:
    """Each vnode's `/tmp` and `/var/tmp`, swept as its session user (whose leftovers they hold)."""
    sweeps = []
    for v in vnodes:
        try:
            pw = pwd.getpwuid(layout.session_uid(v))
        except KeyError:
            continue
        for path in (layout.tmp_dir(v), layout.var_tmp_dir(v)):
            sweeps.append(TmpSweep(path, pw.pw_uid, pw.pw_gid, pw.pw_dir))
    return sweeps


def _changed(st: os.stat_result) -> float:
    return max(st.st_mtime, st.st_ctime)


def newest_change(path: str, entry_cap: int = TMP_WALK_ENTRY_CAP, seconds: float = TMP_WALK_SECONDS,
                  clock: Callable[[], float] = time.monotonic) -> float | None:
    """The newest max(mtime, ctime) of PATH or anything under it, never following a link; None when the walk is
    over `entry_cap` entries or `seconds`, or fails (undeterminable is fresh)."""
    deadline = clock() + seconds
    try:
        newest = _changed(os.lstat(path))
        seen = 0
        for _root, dirs, files, root_fd in os.fwalk(path, follow_symlinks=False):
            for name in dirs + files:
                seen += 1
                if seen > entry_cap or clock() > deadline:
                    return None
                try:
                    newest = max(newest, _changed(os.stat(name, dir_fd=root_fd, follow_symlinks=False)))
                except FileNotFoundError:
                    continue
        return newest
    except OSError:
        return None


def sweep_stale_tmp(tmp: Path, now: Callable[[], float] = time.time, uid: int | None = None) -> dict:
    """`--tmp-self`: run AS THE SESSION USER. Removes each top-level DIRECTORY of ours in TMP in which nothing
    changed for `TMP_STALE_DAYS`, but `TMP_KEEP_PREFIXES`; files, sockets, links and other users' entries stay.
    Returns `{stale_tmp, error}`: bytes actually freed, and the first failure (the rest of TMP still goes)."""
    result: dict[str, Any] = {"stale_tmp": 0, "error": None}
    uid = os.getuid() if uid is None else uid
    cutoff = now() - TMP_STALE_DAYS * 86400
    try:
        if tmp.is_symlink() or not tmp.is_dir():
            return result
        entries = list(os.scandir(tmp))
    except OSError as exc:
        result["error"] = f"tmp sweep failed: {exc.strerror or type(exc).__name__}"
        return result
    for entry in entries:
        if entry.name.startswith(TMP_KEEP_PREFIXES) or entry.name.endswith(f"-{uid}"):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
                continue
            newest = newest_change(entry.path)
            if newest is None or newest > cutoff:
                continue
            size = disk_events.tree_bytes(entry.path)
            shutil.rmtree(entry.path, ignore_errors=True)
            result["stale_tmp"] += max(size - disk_events.tree_bytes(entry.path), 0)
        except OSError as exc:
            result["error"] = result["error"] or f"tmp sweep failed: {exc.strerror or type(exc).__name__}"
    return result


def prune_tarball_cache(cache_dir: Path | None = None) -> int:
    """Keep only the pinned runner tarball (`install.RUNNER_TARBALL`) in the root-owned cache. Bytes freed."""
    cache_dir = install.RUNNER_CACHE_DIR if cache_dir is None else cache_dir
    freed = 0
    try:
        entries = list(os.scandir(cache_dir))
    except OSError:
        return 0
    for entry in entries:
        if entry.name == install.RUNNER_TARBALL or not entry.is_file(follow_symlinks=False):
            continue
        try:
            freed += entry.stat(follow_symlinks=False).st_blocks * 512
            os.unlink(entry.path)
        except OSError:
            pass
    return freed


# ── the Docker Engine API, over the owner's socket ───────────────────────────────────────────────────

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class DockerError(Exception):
    pass


class DockerAPI:
    """The few Engine API calls the rule needs. Every byte figure comes from the daemon itself."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def call(self, method: str, path: str, timeout: float = API_TIMEOUT_S):
        conn = _UnixConnection(self.socket_path, timeout)
        try:
            conn.request(method, path, headers={"Host": "docker"})
            resp = conn.getresponse()
            body = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError(f"engine unreachable ({type(exc).__name__})") from exc
        finally:
            conn.close()
        if resp.status >= 300:
            raise DockerError(f"{method} {path.split('?')[0]} answered {resp.status}")
        return json.loads(body) if body else None

    def df(self) -> dict:
        """Images, BuildKit and containers — never volumes (`VOLUME_DF_TIMEOUT_S` says why)."""
        return self.call("GET", _DF_FAST, timeout=PRUNE_TIMEOUT_S) or {}

    def volumes_df(self) -> dict:
        return self.call("GET", "/system/df?type=volume", timeout=VOLUME_DF_TIMEOUT_S) or {}

    def images_now(self) -> int:
        """Image bytes on disk right now — the eviction loop's measure (`df?type=image` is the cheap one)."""
        return images_bytes(self.call("GET", "/system/df?type=image", timeout=PRUNE_TIMEOUT_S) or {})

    def api_version(self) -> tuple[int, int]:
        """The engine's API version, `(0, 0)` when it does not say."""
        try:
            major, minor = str((self.call("GET", "/version") or {}).get("ApiVersion", "0.0")).split(".")[:2]
            return int(major), int(minor)
        except (DockerError, ValueError, AttributeError):
            return 0, 0

    def snapshot(self) -> Snapshot:
        df = self.df()
        containers = self.call("GET", "/containers/json?all=1") or []
        volumes = (self.call("GET", "/volumes") or {}).get("Volumes") or []
        return snapshot_from_api(df, containers, volumes)


def _is_running(c: dict, now: float) -> bool:
    state = (c.get("State") or "").lower()
    if state == "created":
        created = c.get("Created")
        return not created or now - int(created) <= CREATED_GRACE_S  # no stamp: kept, never guessed old
    return state in _RUNNING_STATES


def snapshot_from_api(df: dict, containers: list, volumes: list, now: float | None = None) -> Snapshot:
    """Pure but for the clock (`now`): the Engine API's fast `/system/df`, `/containers/json?all=1` and
    `/volumes` into a Snapshot."""
    now = time.time() if now is None else now
    size_rw = {c.get("Id"): max(int(c.get("SizeRw") or 0), 0) for c in df.get("Containers") or []}
    cs = tuple(
        Container(
            id=c.get("Id", ""),
            running=_is_running(c, now),
            image_id=c.get("ImageID", ""),
            volumes=tuple(m.get("Name", "") for m in c.get("Mounts") or [] if m.get("Type") == "volume"),
            compose_project=(c.get("Labels") or {}).get(COMPOSE_PROJECT_LABEL, ""),
            size_rw=size_rw.get(c.get("Id"), 0),
        ) for c in containers)
    imgs = tuple(
        Image(
            id=i.get("Id", ""),
            repo_tags=tuple(i.get("RepoTags") or ()),
            repo_digests=tuple(i.get("RepoDigests") or ()),
            created=int(i.get("Created") or 0),
            unique_size=max(int(i.get("Size") or 0) - max(int(i.get("SharedSize") or 0), 0), 0),
            labels=dict(i.get("Labels") or {}),
        ) for i in df.get("Images") or [])
    vs = tuple(Volume(name=v.get("Name", ""), labels=dict(v.get("Labels") or {}),
                      mountpoint=v.get("Mountpoint") or "") for v in volumes)
    return Snapshot(cs, imgs, vs, images_bytes=images_bytes(df), build_cache_bytes=build_cache_bytes(df))


def images_bytes(df: dict) -> int:
    return max(int(df.get("LayersSize") or 0), 0)


def build_cache_bytes(df: dict) -> int:
    return sum(max(int(b.get("Size") or 0), 0) for b in df.get("BuildCache") or [] if not b.get("Shared"))


def volumes_bytes(df: dict) -> int:
    return sum(max(int((v.get("UsageData") or {}).get("Size") or 0), 0) for v in df.get("Volumes") or [])


def volume_bytes(vol: Volume, du: Callable[[str], int] | None = None) -> int:
    """What removing VOL frees: its mountpoint's size, read as the engine's owner (this runs in the child).
    0 when unreadable — a figure for the report, never an input to the rule."""
    return (du or disk_events.tree_bytes)(vol.mountpoint) if vol.mountpoint else 0


def reclaim_engine(api: DockerAPI, engine_class: str, live: frozenset[str] | None,
                   measure_only: bool = False, du: Callable[[str], int] | None = None,
                   quick: bool = False, cap_bytes: int = ENGINE_CACHE_CAP_GB * GB) -> dict:
    """Apply the rule to one engine through ITS API. Returns the engine line's numbers and `freed`. Never
    raises: an engine answering in a shape this does not know is that engine's `error`.

    `quick` (the per-job hook, inside `timeout 120`): no volume sizing at all — the rule is the same, only
    the report figures that need a walk are skipped."""
    try:
        return _reclaim_engine(api, engine_class, live, measure_only, du, quick, cap_bytes)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return {"bytes_before": None, "bytes_after": None, "volumes_bytes_after": None,
                "freed": {k: 0 for k in ("ci_images", "build_cache", "stopped_containers", "orphan_volumes")},
                "error": f"unexpected engine answer ({type(exc).__name__})"}


def _reclaim_engine(api: DockerAPI, engine_class: str, live: frozenset[str] | None, measure_only: bool,
                    du: Callable[[str], int] | None, quick: bool,
                    cap_bytes: int = ENGINE_CACHE_CAP_GB * GB) -> dict:
    out = {"bytes_before": None, "bytes_after": None, "volumes_bytes_after": None,
           "freed": {k: 0 for k in ("ci_images", "build_cache", "stopped_containers", "orphan_volumes")},
           "error": None}
    errors: list[str] = []
    try:
        snap = api.snapshot()
    except DockerError as exc:
        out["error"] = str(exc)
        return out
    out["bytes_before"] = snap.images_bytes + snap.build_cache_bytes
    mid = None
    if not measure_only:
        plan = select_engine_removals(snap, engine_class, live, cap_bytes)
        for cid in plan.containers:
            try:
                api.call("DELETE", f"/containers/{quote(cid, safe='')}?v=0")
                out["freed"]["stopped_containers"] += next((c.size_rw for c in snap.containers if c.id == cid), 0)
            except DockerError as exc:
                errors.append(f"container removal: {exc}")

        def delete_image(img: Image) -> bool:
            # By TAG, never `force`: the engine itself then refuses an image any container uses (a job's
            # `docker create` that landed after the snapshot). The last tag's removal removes the image.
            refs = [t for t in img.repo_tags if t and t != "<none>:<none>"] or [img.id]
            try:
                for ref in refs:
                    api.call("DELETE", f"/images/{quote(ref, safe='')}?noprune=0")
                return True
            except DockerError as exc:
                if "409" not in str(exc):   # in use: the engine said no, which is the point
                    errors.append(f"image removal: {exc}")
                return False

        for img in plan.images:
            delete_image(img)
        evict_until_cap(plan.evict, api.images_now, delete_image, cap_bytes=cap_bytes)
        by_name = {v.name: v for v in snap.volumes}
        for name in plan.volumes:
            size = 0 if quick or name not in by_name else volume_bytes(by_name[name], du)
            try:
                api.call("DELETE", f"/volumes/{quote(name, safe='')}")
                out["freed"]["orphan_volumes"] += size
            except DockerError as exc:
                if "409" not in str(exc):
                    errors.append(f"volume removal: {exc}")
        try:
            mid = api.df()
            target = build_cache_target(images_bytes(mid), cap_bytes=cap_bytes)
            # API 1.48 split `keep-storage` into a floor (`reserved-space`) and a ceiling (`max-used-space`);
            # a ceiling is what "prune down to the target" means there.
            limit = "max-used-space" if api.api_version() >= (1, 48) else "keep-storage"
            api.call("POST", f"/build/prune?all=1&{limit}={target}", timeout=PRUNE_TIMEOUT_S)
            out["freed"]["ci_images"] = max(snap.images_bytes - images_bytes(mid), 0)
        except DockerError as exc:
            errors.append(f"build cache prune: {exc}")
            mid = None
    try:
        after = api.df()
        out["bytes_after"] = images_bytes(after) + build_cache_bytes(after)
        if not measure_only:
            out["freed"]["build_cache"] = max(snap.build_cache_bytes - build_cache_bytes(after), 0)
            if mid is None:
                out["freed"]["ci_images"] = max(snap.images_bytes - images_bytes(after), 0)
    except DockerError as exc:
        errors.append(f"measure: {exc}")
    try:
        if not quick:
            out["volumes_bytes_after"] = volumes_bytes(api.volumes_df())
    except DockerError:
        # Slow or unreachable: no figure, and not an error — the rule never reads it.
        pass
    if errors:
        out["error"] = "; ".join(errors[:3])
    return out


# ── the report (pinned contract §3.1) ────────────────────────────────────────────────────────────────

def trigger_for(used: float, warn: float | None) -> str:
    return "warn" if warn is not None and used >= warn else "scheduled"


def build_report(*, reclaim_id: str, started: float, finished: float, trigger: str, used_before: float,
                 used_after: float, freed: dict, engines: list[dict], errors: list[str],
                 remaining: dict | None = None) -> dict:
    """The `POST /api/agent/disk-reclaims` body. Only this module's own `freed_bytes` keys are sent;
    `remaining` is what the pass measured and keeps (`pool_mirrors`)."""
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}")
    return {
        "reclaim_id": reclaim_id,
        "started_at": env_helpers.now_iso(started),
        "finished_at": env_helpers.now_iso(finished),
        "trigger": trigger,
        "used_pct_before": used_before,
        "used_pct_after": used_after,
        "freed_bytes": {**{k: int(freed.get(k, 0) or 0) for k in OWN_FREED_KEYS},
                        **{k: int(freed[k]) for k in SPARSE_FREED_KEYS if int(freed.get(k, 0) or 0) > 0}},
        "engines": engines,
        "removed_users": [],
        "remaining_bytes": {k: int(v) for k, v in (remaining or {}).items()},
        "errors": [scrub_error(e) for e in errors][:20],
    }


def scrub_error(text: str) -> str:
    """One short operator sentence: no absolute path (names only), bounded length."""
    words = [w.rsplit("/", 1)[-1] if "/" in w and w.startswith("/") else w for w in str(text).split()]
    return " ".join(words)[:200]


# ── running as an owner ──────────────────────────────────────────────────────────────────────────────

def owner_argv(uid: int, gid: int, home: str, socket_path: str, args: list[str]) -> list[str]:
    """This file's child mode under the owner's uid, with a clean environment naming only its engine."""
    env = ["env", "-i", f"HOME={home}", "PATH=/usr/bin:/bin", f"DOCKER_HOST=unix://{socket_path}"]
    if uid != 0:
        env.insert(3, f"XDG_RUNTIME_DIR=/run/user/{uid}")
        return [SETPRIV, f"--reuid={uid}", f"--regid={gid}", "--init-groups", *env,
                PYTHON, "-I", str(Path(__file__).resolve()), *args]
    return [*env, PYTHON, "-I", str(Path(__file__).resolve()), *args]


# A child's whole answer is one short JSON line; anything longer is not ours.
_CHILD_OUTPUT_CAP = 65536


def _count(value) -> int | None:
    """A byte figure from a child, or None: a child runs as an untrusted owner (its user site, its dockerd),
    so nothing it prints reaches the report unless it is a sane non-negative int."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 62 else None


def run_child(argv: list[str], run: Callable = subprocess.run) -> dict:
    """The child's ONE JSON object, or `{"error": ...}` — validated by the callers through `_count`, its error
    through `scrub_error`. `-I` keeps the owner's user site and PYTHON* environment out of the child."""
    try:
        proc = run(argv, capture_output=True, text=True, timeout=CHILD_TIMEOUT_S, check=False, cwd="/")
    except subprocess.TimeoutExpired:
        return {"error": "timed out"}
    except OSError as exc:
        return {"error": f"could not start ({type(exc).__name__})"}
    lines = (proc.stdout or "")[-_CHILD_OUTPUT_CAP:].strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else {"error": f"no result (exit {proc.returncode})"}
    except ValueError:
        return {"error": f"unreadable result (exit {proc.returncode})"}
    return result if isinstance(result, dict) else {"error": "unreadable result"}


def _child_error(result: dict) -> str | None:
    err = result.get("error")
    return scrub_error(err) if isinstance(err, str) and err else None


def engine_args(engine: Engine, live: frozenset[str] | None, measure_only: bool,
                cap_bytes: int | None = None) -> list[str]:
    args = ["--engine-self", "--class", engine.engine_class]
    if cap_bytes is not None:
        args += ["--cap-bytes", str(cap_bytes)]
    if engine.engine_class == CLASS_SESSION:
        args += ["--live-unknown"] if live is None else [f"--live={','.join(sorted(live))}"]
    if measure_only:
        args.append("--measure-only")
    return args


# ── the pass ─────────────────────────────────────────────────────────────────────────────────────────

def host_jaah_home() -> str | None:
    """The host's JAAH_HOME (new or legacy name), from the unit's EnvironmentFile; None when unset."""
    try:
        import environment  # noqa: PLC0415 — stdlib-only leaf, read lazily
        return environment.pool_root_or_empty().rstrip("/") or None
    except Exception:  # noqa: BLE001 — an unreadable root is "could not enumerate"
        return None


def pool_mirror_roots(vnodes: Iterable[layout.Vnode] = ()) -> list[Path]:
    """Every static-pool git mirror root on this host (`git_mirror.mirrors_root(shared=True)`): the host
    daemon's and each vnode daemon's. Resolved here, not by `git_mirror`: this pass runs as root, whose
    `$HOME` is not the daemon's state dir."""
    try:
        import environment  # noqa: PLC0415 — stdlib-only leaf, read lazily
        relocated = environment.pool_mirrors_root()
    except Exception:  # noqa: BLE001 — an unreadable env is the default root
        relocated = ""
    host = Path(relocated) if relocated else Path(env_helpers.DAEMON_STATE_DIR_DEFAULT) / "pool-mirrors"
    return [host, *(Path(layout.mirrors_dir(v)) for v in vnodes)]


def pool_mirrors_bytes(roots: Iterable[Path]) -> int | None:
    """Bytes the pool mirrors hold (removed by jaahd's roster sweep, never by this pass), or None when no
    root exists — a node with no static pool reports no line."""
    present = [root for root in roots if root.is_dir() and not root.is_symlink()]
    return sum(disk_events.tree_bytes(root) for root in present) if present else None


def run_pass(*, run: Callable = subprocess.run, now: Callable[[], float] = time.time,
             vnodes: list[layout.Vnode] | None = None, engines: list[Engine] | None = None,
             trees: list[RunnerTree] | None = None, busy: frozenset[int] | None | str = "probe",
             host_live: frozenset[str] | None | str = "probe",
             statvfs: Callable = os.statvfs, warn_pct_file: Path = disk_events.WARN_PCT_FILE,
             tarball_cache: Path | None = None, mirror_roots: list[Path] | None = None,
             tmp_sweeps: list[TmpSweep] | None = None) -> dict:
    """ONE reclaim pass over every engine and persistent runner tree on this host. Never raises for an
    engine's or tree's failure: each lands in `errors` and the rest of the pass goes on."""
    started = now()
    errors: list[str] = []
    used_before = disk_events.used_pct("/", statvfs)
    warn_pct = disk_events.read_warn_pct(warn_pct_file)
    trigger = trigger_for(used_before, warn_pct)
    try:
        st = statvfs("/")
        cap_bytes = engine_cap_bytes(st.f_blocks * st.f_frsize, used_before, warn_pct)
    except OSError:
        cap_bytes = ENGINE_CACHE_CAP_GB * GB
    if vnodes is None:
        try:
            vnodes = vnode_records()
        except (OSError, ValueError) as exc:
            errors.append(f"vnode store unreadable ({type(exc).__name__}); vnode engines treated as host")
            vnodes = []
    def busy_now(uid: int) -> bool:
        # Asked again right before each runner child, never once for the pass: a pass spends minutes on
        # engines, and a runner idle at its start may have taken a job since.
        return is_busy(uid, busy_uids() if busy == "probe" else busy)

    host_set = live_projects(host_jaah_home()) if host_live == "probe" else host_live
    vnode_live = {v: live_projects(layout.jaah_home(v)) for v in vnodes}
    engines = enumerate_engines(vnodes) if engines is None else engines
    trees = enumerate_runner_trees(vnodes) if trees is None else trees
    tmp_sweeps = enumerate_tmp_sweeps(vnodes) if tmp_sweeps is None else tmp_sweeps

    freed = {k: 0 for k in OWN_FREED_KEYS + SPARSE_FREED_KEYS}
    lines: list[dict] = []
    for engine in engines:
        live = live_for_engine(engine, host_set, vnode_live)
        skipped = engine.engine_class == CLASS_RUNNER and busy_now(engine.uid)
        result = run_child(owner_argv(engine.uid, engine.gid, engine.home, engine.socket,
                                      engine_args(engine, live, skipped,
                                                  engine_cap_for(engine, vnodes, cap_bytes))), run)
        child_freed = result.get("freed") if isinstance(result.get("freed"), dict) else {}
        for k in ("ci_images", "build_cache", "stopped_containers", "orphan_volumes"):
            freed[k] += _count(child_freed.get(k)) or 0
        error = _child_error(result)
        if error:
            errors.append(f"{engine.user}: {error}")
        lines.append({"user": engine.user, "uid": engine.uid,
                      "bytes_before": _count(result.get("bytes_before")),
                      "bytes_after": _count(result.get("bytes_after")),
                      "volumes_bytes_after": _count(result.get("volumes_bytes_after")),
                      "skipped": "busy" if skipped else None, "error": error})
    for tree in trees:
        if busy_now(tree.uid):
            continue
        result = run_child([SETPRIV, f"--reuid={tree.uid}", f"--regid={tree.gid}", "--init-groups",
                            "env", "-i", f"HOME={tree.home}", "PATH=/usr/bin:/bin",
                            PYTHON, "-I", str(Path(__file__).resolve()), "--tree-self", tree.path], run)
        freed["runner_versions"] += _count(result.get("runner_versions")) or 0
        freed["runner_temp"] += _count(result.get("runner_temp")) or 0
        error = _child_error(result)
        if error:
            errors.append(f"{tree.user} {os.path.basename(tree.path)}: {error}")
    for sweep in tmp_sweeps:
        result = run_child([SETPRIV, f"--reuid={sweep.uid}", f"--regid={sweep.gid}", "--init-groups",
                            "env", "-i", f"HOME={sweep.home}", "PATH=/usr/bin:/bin",
                            PYTHON, "-I", str(Path(__file__).resolve()), "--tmp-self", sweep.path], run)
        freed["stale_tmp"] += _count(result.get("stale_tmp")) or 0
        error = _child_error(result)
        if error:
            errors.append(f"{os.path.basename(sweep.path)} of uid {sweep.uid}: {error}")
    freed["runner_versions"] += prune_tarball_cache(tarball_cache)
    mirrors = pool_mirrors_bytes(pool_mirror_roots(vnodes) if mirror_roots is None else mirror_roots)

    finished = now()
    return build_report(reclaim_id=str(uuid.uuid4()), started=started, finished=finished, trigger=trigger,
                        used_before=used_before, used_after=disk_events.used_pct("/", statvfs), freed=freed,
                        engines=lines, errors=errors,
                        remaining={} if mirrors is None else {"pool_mirrors": mirrors})


def _main_pass() -> int:
    try:
        import environment  # noqa: PLC0415 — stdlib-only leaf: the node runtime root
        Path(environment.run_path(*disk_reclaim_trigger.MARKER_PARTS)).unlink(missing_ok=True)
    except (OSError, RuntimeError):
        pass
    try:
        report = run_pass()
        path = disk_events.write_report(report)
        disk_events.trim_spool()
        print(f"jaah-reclaim: {report['trigger']} pass {report['reclaim_id']}: "
              f"{report['used_pct_before']}% → {report['used_pct_after']}%, "
              f"{len(report['engines'])} engine(s), {len(report['errors'])} error(s) → "
              f"{path.name if path else 'no spool (report dropped)'}")
    except Exception as exc:  # noqa: BLE001 — a root oneshot that fails red tells nobody; the next pass retries
        print(f"jaah-reclaim: pass failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


def _main_engine_self(args: argparse.Namespace) -> int:
    """Run as an engine's owner. With no `--class` this is the per-job hook: classify ourselves, and step
    aside while another job of this uid is running on the same engine."""
    socket_path = os.environ.get("DOCKER_HOST", "").removeprefix("unix://") or f"/run/user/{os.getuid()}/docker.sock"
    engine_class = args.engine_class
    hook = engine_class is None
    measure_only = args.measure_only
    if hook:
        try:
            engine_class = classify_owner(pwd.getpwuid(os.getuid()).pw_name)
        except KeyError:
            engine_class = CLASS_SESSION
        if other_worker_busy(os.getuid(), os.getpid()):
            print(json.dumps({"error": None, "skipped": "busy"}))
            return 0
    elif engine_class == CLASS_RUNNER and install.uid_busy(os.getuid()):
        # The root pass checked a moment ago; a job may have started since. Measure, remove nothing.
        measure_only = True
    live = None if args.live_unknown or args.live is None else frozenset(p for p in args.live.split(",") if p)
    # The root pass hands its figure down (that is how the scaled cap reaches engines); the per-job hook has
    # none and applies the SAME rule, but a persistent runner's user cannot read the warn file, so there it
    # stays the base cap. Clamped to the floor: a bad value is never a tighter cap.
    cap = host_engine_cap_bytes() if args.cap_bytes is None else max(args.cap_bytes, ENGINE_CACHE_CAP_GB * GB)
    print(json.dumps(reclaim_engine(DockerAPI(socket_path), engine_class, live, measure_only, quick=hook,
                                    cap_bytes=cap)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pass", dest="do_pass", action="store_true", help="one root pass (jaah-reclaim.service)")
    mode.add_argument("--engine-self", action="store_true", help="this user's own engine (the per-job hook)")
    mode.add_argument("--tree-self", metavar="TREE", help="this user's own runner tree")
    mode.add_argument("--tmp-self", metavar="TMP", help="this user's stale directories in a vnode's tmp")
    parser.add_argument("--class", dest="engine_class", choices=(CLASS_RUNNER, CLASS_SESSION))
    parser.add_argument("--live", default=None, help="comma-separated live slot projects (session engines)")
    parser.add_argument("--live-unknown", action="store_true", help="live slot projects could not be enumerated")
    parser.add_argument("--cap-bytes", type=int, default=None, help="per-engine cap (the root pass's figure)")
    parser.add_argument("--measure-only", action="store_true", help="report bytes, remove nothing")
    args = parser.parse_args(argv)
    if args.do_pass:
        if os.geteuid() != 0:
            print("jaah-reclaim: --pass runs as root (jaah-reclaim.service)", file=sys.stderr)
            return 2
        return _main_pass()
    if args.engine_self:
        return _main_engine_self(args)
    if args.tmp_self:
        print(json.dumps(sweep_stale_tmp(Path(args.tmp_self))))
        return 0
    print(json.dumps(apply_tree_cleanup(Path(args.tree_self))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
