#!/usr/bin/env python3
"""shared_storage.py — where this node keeps and reads an account's TOKEN.

THE RULE (`token_dir`): the token store `{jaah-home}/auth/.shared/<slug>` (`SHARED_ACCOUNTS_SEGMENT`;
`.shared` can never be an account slug) when it holds a credential, whether or not a NodeLink is mounted
there; else the account's config dir `{jaah-home}/auth/<slug>`. The store goes first so an account whose
token moved to the store keeps using it past a credential left behind in its config dir. Every login mints into the store, so an org that attaches
a NodeLink at `{jaah-home}/auth/.shared` shares its tokens across nodes; with nothing mounted the store is
local disk and the account is this node's alone. Nothing here asks the CP whether an account is shared.
`CLAUDE_SECURESTORAGE_CONFIG_DIR` moves only Claude's token; the config dir stays per-node. A
`shared_token_link` provider has no such variable, so its credential file in the per-node home is a symlink
into the store (`fleet_dispatch.ensure_shared_credential_link`). A hand-configured legacy share at
`LEGACY_SHARED_ACCOUNTS_ROOT` stands in for the store while `.shared` is not mounted (`account_token_root`).
A symlinked `.shared` or leaf is refused, never followed — except, on macOS, a NodeLink path that is root's link
to the staging directory holding its mount (`_darwin_staging_target`), the one shape that OS mounts in. The `<root>/<slug>` rule is mirrored in
`apps/api/app/shared_storage.py` (the daemon and the CP never import each other); `tests/test_shared_storage.py` is the oracle.

The access point forces uid 3000:3000 on every file of a mounted store, so nothing may compare a credential
file's owner there against the session uid — 3000 is the correct answer.

WINDOWS: the store is `{jaah-home}\\auth\\.shared\\<slug>`, and a NodeLink there is not a mount but a directory
SYMLINK into the `jaah-engine` WSL2 distro, which mounts the access point (`nodelink_windows.py`). "Mounted"
is that link naming exactly `WINDOWS_NODELINK_TARGET_ROOT\\<id>` AND the daemon's status record saying
mounted (`_windows_mounted_id`). `\\\\wsl.localhost` is per-user, so this daemon never reads through the link
— only the runner account, which owns the distro, does: a store under such a link is accepted lexically
(`store_is_on_share`), and a credential there this daemon cannot read is present (`credential_file_present`).

STDLIB ONLY: `agent_config_dirs.py` (a hook-loaded leaf) imports this; `environment` is a stdlib leaf too.
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import re
import secrets
import stat
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath

import cloud_modules
import environment
import file_io
import host_platform

log = logging.getLogger("jaahd.shared_storage")

# ── The pinned contract (mirror of apps/api/app/shared_storage.py) ────────────────────────────────────

# The token store, joined to `JAAH_HOME`. A leading `.` keeps it out of the account-slug space
# (`^[a-z0-9]…`), so it can never collide with a per-node config dir beside it.
SHARED_ACCOUNTS_SEGMENT = "auth/.shared"
# The pre-NodeLink mount, still mounted (same access point) while nodes move over; never dropped here.
LEGACY_SHARED_ACCOUNTS_ROOT = "/mnt/jaah-shared/accounts"

# The uid (and gid) an EFS access point forces on every file of a mounted store (the CP creates every
# NodeLink access point with it; `vnodehelper/ops.py::NODELINK_OWNER_ID` is the helper's own copy).
ACCESS_POINT_UID = 3000

# THE SINGLE HOME OF THIS SPELLING (a typo is an unauthenticated runner).
SECURESTORAGE_ENV = "CLAUDE_SECURESTORAGE_CONFIG_DIR"



def _credential_files() -> tuple[str, ...]:
    """Every provider's credential file relative to the directory a store or a CLI home names — the
    catalogue's `credential_file`, never re-spelled here.

    One slug is one provider, so any of them present means "this store holds the account's credential". Only
    the DIRECTORY question is consolidated here (`token_dir`). Lazy import: this module is a hook-loaded leaf."""
    import agent_providers  # noqa: PLC0415 — deferred, see the docstring

    # A provider with no credential file (an API-key-only one) names nothing here: `""` would join to the
    # directory itself and read as a credential that is present.
    return tuple(p.credential_file for p in agent_providers.PROVIDERS if p.credential_file)


def _has_control_character(value: str) -> bool:
    return any(ch < " " or ch == "\x7f" or "\x80" <= ch <= "\x9f" or ch in "\u2028\u2029" for ch in value)


def windows_absolute(raw: str) -> str:
    """RAW as a Windows absolute path without its trailing separators, or `""` — `nodelink_mount.windows_path`,
    the one rule for a Windows node's paths."""
    nodelink_mount = cloud_modules.load("nodelink_mount")  # deferred: Windows only, and this module is a hook-loaded leaf

    return nodelink_mount.windows_path(raw) if nodelink_mount is not None else ""


def accounts_nodelink_root() -> str:
    """`{JAAH_HOME}/auth/.shared` — the token store's path on this node, mounted or not — or `""` with no
    usable home. On Windows `{JAAH_HOME}\\auth\\.shared`, for a drive-absolute home."""
    if host_platform.is_windows():
        home = windows_absolute(environment.pool_root_or_empty())
        return ntpath.join(home, *SHARED_ACCOUNTS_SEGMENT.split("/")) if home else ""
    home = environment.pool_root_or_empty().rstrip("/")
    if not home.startswith("/") or ".." in home.split("/") or _has_control_character(home):
        return ""
    return f"{home}/{SHARED_ACCOUNTS_SEGMENT}"


#: How long one mount check may take before the mount counts as NOT mounted. A `hard` NFS mount whose server is
#: gone — a node moved to another environment's tailnet keeps the old one's EFS mounted — blocks `stat` forever,
#: and jaahd asks from its event loop: one unbounded `ismount` froze every periodic step, the vnode converge
#: included. Generous, so a live EFS riding out a brownout is not read as gone.
MOUNT_CHECK_TIMEOUT = 10.0
# A path whose check is in flight: its thread, its answer and when it gives up. A second caller waits on that
# check instead of starting another; a check still blocked past its deadline answers False at once until its
# thread returns or mountinfo stops listing the path (it was detached) — so a dead mount costs one timeout,
# never one per call.
_mount_checks: dict[str, tuple[threading.Thread, list[bool], float]] = {}
_mount_checks_lock = threading.Lock()
#: While a check stays blocked, its warning repeats at this cadence: the CP turns the shipped line into the node's
#: "NodeLink store not answering" Health finding (`apps/api/app/node_log_findings.py`), which lapses when the line
#: stops arriving — so a hang that is still there must keep saying so, not warn once and fall silent.
MOUNT_HUNG_REWARN = 300.0
# The last warning per path, keyed by the check it was for: a new check starts its own cadence.
_mount_warned: dict[str, tuple[threading.Thread, float]] = {}
# Bound at import: a test that fakes `threading.Thread` or `time.monotonic` for its own subject must not reach
# this check.
_Thread = threading.Thread
_monotonic = time.monotonic


# macOS has no mountinfo: its kernel table is `getfsstat` (`_darwin_mounts`). Hooks, so a test drives that branch.
_is_macos = host_platform.is_macos


def _darwin_mounts() -> list | None:
    """macOS's mount table as `nodelink_mount.MountFacts` (`getfsstat` with MNT_NOWAIT: never asks a server), or
    None when it cannot be read. The binding's single home is the root helper, which runs `-I` and so cannot
    import this side; imported lazily, on macOS only."""
    nodelink_mount = cloud_modules.load("nodelink_mount")  # deferred: macOS only, and this module is a hook-loaded leaf
    if nodelink_mount is None:
        return None
    try:
        return nodelink_mount.darwin_mounts()
    except OSError:
        return None


def _darwin_key(path: str) -> str:
    """PATH as macOS's mount table spells a mount point (`/var/…` is `/private/var/…`): `host_platform.macos`'s
    rule, through the root helper's copy (importing the macOS seam here would put its `fcntl` in every bundle)."""
    nodelink_mount = cloud_modules.load("nodelink_mount")  # deferred, as in `_darwin_mounts`

    return nodelink_mount.darwin_canonical(path.rstrip("/") or "/") if nodelink_mount is not None else path


def _mountinfo_rows(table: str = "") -> Iterator[tuple[str, str, str, str, str]]:
    """Every entry of the mountinfo(5) TABLE (`_MOUNTINFO`) as `(mount id, mount point, fstype, source, super
    options)` — the fields before the ` - ` separator, then the three after it — with the mount point's octal
    escapes (`\\040` space, `\\011` tab, `\\134` backslash) undone once, here. The one reader of the kernel's table:
    nothing else here parses a line. A line short of those fields is skipped; an unreadable table raises
    `OSError`, since each caller has its own "no table" answer."""
    with open(table or _MOUNTINFO, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    for line in lines:
        head, sep, tail = line.partition(" - ")
        fields, after = head.split(), tail.split()
        if not sep or len(fields) < 5 or len(after) < 3:
            continue
        point = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
        yield fields[0], point, after[0], after[1], after[2]


def _listed_as_mount(path: str, *, no_table: bool = True) -> bool:
    """Does mountinfo list `path`, exactly as given, as a mount point? Asks the kernel's table, never the
    filesystem, so it cannot block on a dead server. `no_table` when there is no table to read (not Linux
    or macOS, or an unreadable one)."""
    if _is_macos():
        table = _darwin_mounts()
        want = _darwin_key(path)
        return no_table if table is None else any(m.on == want for m in table)
    try:
        return any(point == path for _, point, *_ in _mountinfo_rows())
    except OSError:
        return no_table


def mounts_under(path: str) -> list[str]:
    """Every mount point mountinfo lists AT or BELOW `path` (compared as its realpath) — what a recursive
    removal of `path` would cross into. Asks the kernel's table, never the filesystem; `[]` with no table
    (not Linux or macOS). On Windows: every NodeLink link still this node's own (`_windows_links`) at or below
    `path`, mounted or not — a link into the engine is a crossing either way."""
    if host_platform.is_windows():
        base = ntpath.normcase(path).rstrip("\\")
        return [link for link in _windows_links()
                if ntpath.normcase(link) == base or ntpath.normcase(link).startswith(base + "\\")]
    if _is_macos():
        base = _darwin_key(os.path.realpath(path)).rstrip("/") or "/"
        return [m.on for m in _darwin_mounts() or () if m.on == base or m.on.startswith(base + "/")]
    try:
        rows = list(_mountinfo_rows())
    except OSError:
        return []
    base = os.path.realpath(path).rstrip("/") or "/"
    return [point for _, point, *_ in rows if point == base or point.startswith(base + "/")]


def _start_mount_check(path: str) -> tuple[threading.Thread, list[bool], float]:
    answer: list[bool] = []

    def check() -> None:
        try:
            answer.append(os.path.ismount(path))
        except OSError:
            answer.append(False)

    worker = _Thread(target=check, name="mount-check", daemon=True)
    worker.start()
    return worker, answer, _monotonic() + MOUNT_CHECK_TIMEOUT


def _is_mount(path: str) -> bool:
    """`os.path.ismount`, bounded by `MOUNT_CHECK_TIMEOUT`: a mount that does not answer is not one (fail-closed,
    as every caller reads False as "no shared store here"). On Windows: a mounted NodeLink link
    (`_windows_mounted_id`), which reads the link and the status file only."""
    if not path:
        return False
    if host_platform.is_windows():
        return bool(_windows_mounted_id(path))
    with _mount_checks_lock:
        check = _mount_checks.get(path)
        if check is None or not check[0].is_alive() or not _listed_as_mount(path):
            check = _mount_checks[path] = _start_mount_check(path)
    worker, answer, deadline = check
    worker.join(max(0.0, deadline - _monotonic()))
    if worker.is_alive():
        with _mount_checks_lock:
            warned = _mount_warned.get(path)
            now = _monotonic()
            due = warned is None or warned[0] is not worker or now - warned[1] >= MOUNT_HUNG_REWARN
            if due:
                _mount_warned[path] = (worker, now)
        if due:
            log.warning("%s did not answer a mount check in %.0fs (a dead NFS server?) — treated as not mounted "
                        "until it does", path, MOUNT_CHECK_TIMEOUT)
        return False
    with _mount_checks_lock:
        if _mount_checks.get(path) is check:
            del _mount_checks[path]
        _mount_warned.pop(path, None)
    return bool(answer and answer[0])


# A typed NFS NodeLink (`nodelink_mount.py`) shows at its path as root's bindfs view of its staging
# mount — accepted only with both layers proven (`_bindfs_layers_up`).
_BINDFS_FSTYPE = "fuse.bindfs"
# The filesystem types a token-store mount at `.shared` may be (EFS mounts as NFSv4).
_ACCOUNTS_FSTYPES = frozenset({"nfs4", "nfs", "efs", _BINDFS_FSTYPE})
_MOUNTINFO = "/proc/self/mountinfo"


def _mount_fstype(path: str) -> str | None:
    """The filesystem type mounted AT `path` (`/proc/self/mountinfo`, last match wins), `""` when no entry
    names it, None when there is no mountinfo to read (neither Linux nor macOS).

    macOS: the kernel's table (`getfsstat`), where a mount a non-root user made — a disk image, `mount_smbfs`,
    a user NFS mount are all possible there without root — reads as `""`, as does an unreadable table: never
    the store (fail closed)."""
    if _is_macos():
        want, found = _darwin_key(os.path.realpath(path)), ""
        for mount in _darwin_mounts() or ():
            if mount.on == want:
                found = mount.fstype if mount.owner == 0 else ""
        return found
    try:
        rows = list(_mountinfo_rows())
    except OSError:
        return None
    want = os.path.realpath(path)
    return next((fstype for _, point, fstype, *_ in reversed(rows) if point == want), "")


def _mount_super_options(path: str) -> str:
    """The super options of the mount AT `path` (mountinfo's field after the source, last match wins), `""` when
    none or unreadable. For FUSE they carry the MOUNTING uid (`user_id=`), which no `fsname` can fake."""
    try:
        rows = list(_mountinfo_rows())
    except OSError:
        return ""
    want = os.path.realpath(path)
    return next((options for _, point, _, _, options in reversed(rows) if point == want), "")


def _bindfs_layers_up(path: str, ident: str) -> bool:
    """A typed NFS NodeLink (IDENT's) at PATH is mounted only as BOTH layers: PATH root's bindfs view (`user_id=0`
    — `auth/` is group-writable and `fusermount` is setuid, so a runner's own bindfs there reads as its uid), over
    root's NFS mount at that NodeLink's staging directory (`nodelink_mount.STAGING_ROOT/<id>`). Either one missing,
    or no id to find the staging by, is not mounted (fail closed)."""
    nodelink_mount = cloud_modules.load("nodelink_mount")  # deferred, as in `_darwin_mounts`

    if nodelink_mount is None or not _NODELINK_ID_RE.match(ident or "") or "user_id=0" not in _mount_super_options(path).split(","):
        return False
    return _mount_fstype(f"{nodelink_mount.STAGING_ROOT}/{ident}") in _NFS_FSTYPES


def _darwin_staging_target(path: str, ident: str = "") -> str:
    """macOS: the staging directory a NodeLink path links to, when it is the sanctioned shape — PATH itself a
    symlink whose one `readlink` is exactly `nodelink_mount.macos_staging_path(MACOS_STAGING_ROOT, <id>)` (IDENT's,
    when given), and
    the kernel's table shows root's NFS mount with nosuid/nodev/noexec at that directory — else `""`. Nothing is
    resolved further: the staging chain is root's alone. Any other link at a NodeLink path is not a mount."""
    nodelink_mount = cloud_modules.load("nodelink_mount")  # deferred, as in `_darwin_mounts`
    if nodelink_mount is None:
        return ""
    try:
        if not stat.S_ISLNK(os.lstat(path).st_mode):
            return ""
        target = os.readlink(path)
    except OSError:
        return ""
    root = nodelink_mount.MACOS_STAGING_ROOT
    leaf = target[len(root) + 1:].split("/", 1)[0] if target.startswith(root + "/") else ""
    if (not _NODELINK_ID_RE.match(leaf) or (ident and leaf != ident)
            or target != nodelink_mount.macos_staging_path(root, leaf)):
        return ""
    required = nodelink_mount._MACOS_REQUIRED
    return target if any(m.on == target and m.fstype == "nfs" and m.owner == 0 and m.flags & required == required
                         for m in _darwin_mounts() or ()) else ""


def _nodelink_id_at(path: str) -> str:
    """The id of the cached NodeLink whose path is PATH, or `""` when the cache names none (so not knowable)."""
    path = path.rstrip("/")
    return next((link["id"] for link in read_nodelinks() if link["path"] == path and link["id"]), "")


def _token_share_target(root: str) -> str:
    """macOS: the staging mount the token share ROOT links to — that of the NodeLink the cache places at ROOT,
    when it names one (`_darwin_staging_target`); `""` on any other OS or shape."""
    return _darwin_staging_target(root, _nodelink_id_at(root)) if _is_macos() else ""


def resolved_store_root(root: str) -> str:
    """The directory to open for the token store ROOT: ROOT itself, or on macOS its link's validated staging mount
    (`_token_share_target`), `""` for any other link there. What a removal opens `O_NOFOLLOW`."""
    if _is_macos() and os.path.islink(root):
        return _token_share_target(root)
    return root


def _nodelink_mounted(path: str, ident: str = "") -> bool:
    """Is a NodeLink (IDENT's, when given) mounted at PATH right now, answering? Linux: PATH is a mount
    (`_is_mount`) — a bindfs one only with both its layers (`_bindfs_layers_up`). macOS: PATH is root's link to its
    staging mount (`_darwin_staging_target`), which answers."""
    if not _is_macos():
        if not _is_mount(path):
            return False
        if host_platform.is_windows() or _mount_fstype(path) != _BINDFS_FSTYPE:
            return True
        return _bindfs_layers_up(path, ident or _nodelink_id_at(path))
    target = _darwin_staging_target(path, ident)
    return bool(target) and _is_mount(target)


def _is_accounts_nodelink_mount(path: str) -> bool:
    """A mount at `{JAAH_HOME}/auth/.shared` that is really NFS (EFS), never one the session user could plant:
    `auth/` is group-writable, so a FUSE mount there would otherwise be adopted as the fleet credential store.
    macOS: only root's link to its staging mount (`_nodelink_mounted`); a host with neither table falls back to
    `ismount`."""
    if _is_macos():
        return _nodelink_mounted(path, _nodelink_id_at(path))
    if not _is_mount(path):
        return False
    if host_platform.is_windows():
        return True   # a mounted NodeLink link (`_is_mount`): the engine's NFS mount, and no mountinfo to ask
    fstype = _mount_fstype(path)
    if fstype == _BINDFS_FSTYPE:
        return _bindfs_layers_up(path, _nodelink_id_at(path))
    return fstype is None or fstype in _ACCOUNTS_FSTYPES


def configured_shared_accounts_root() -> str:
    """The LEGACY shared tree as this node's posture configures it — the override, else the literal, else `""`
    in sandbox (`environment.shared_accounts_root`) — with NO mount test. The surface, not the answer: what
    reads a credential uses `account_token_root()`."""
    return environment.shared_accounts_root(LEGACY_SHARED_ACCOUNTS_ROOT)


def shared_accounts_surface() -> bool:
    """Does this node have a shared account tree AT ALL (mounted or not)? False only in sandbox posture with no
    override — `shared_accounts_root()`'s `""` cannot tell "off" from "not mounted", and the console must."""
    return bool(configured_shared_accounts_root())


def shared_accounts_root() -> str:
    """The token store as a SHARED (mounted) tree on this node right now, or `""` when none is mounted — what
    the snapshot reports as `shared_accounts_root` (the CP fans a login's converge out to nodes reporting one).
    Not where a token is read or minted: that is `account_token_root`, which falls back to local disk.

    1. a NodeLink at `{JAAH_HOME}/auth/.shared`, if it is an NFS MOUNT (`_is_accounts_nodelink_mount`);
    2. else the legacy root — `$JAAH_SHARED_ACCOUNTS_ROOT` when set (a co-located node's own legacy mount, or a
       test's tree), else `LEGACY_SHARED_ACCOUNTS_ROOT` — either only if it is a MOUNT: a co-located node whose
       store could not mount comes up without it (`vnodehelper.ops._mount_shared_soft`) and must not report a
       share it does not have;
    3. else `""`. Never a local-disk answer: an unmounted mount point is a plain local directory.

    Sandbox posture with no override is `""` (`environment.shared_accounts_root`). The literals stay in THIS
    file: three guards `ast.literal_eval` them without importing. Resolved per call."""
    legacy = configured_shared_accounts_root()
    if not legacy:
        return ""
    nodelink = accounts_nodelink_root()
    if _is_accounts_nodelink_mount(nodelink):
        return nodelink
    return legacy if _is_mount(legacy) else ""


def account_token_root() -> str:
    """THE ONE RESOLVER of the token store's root on this node, or `""` when there is none to trust right now.

    1. `{JAAH_HOME}/auth/.shared` when it is an NFS (EFS) mount that answers (`_is_accounts_nodelink_mount`);
       else `""` while mountinfo still lists a mount there — a hung server would block every read,
       and a non-NFS mount there is one the session user could plant (`auth/` is
       group-writable);
    2. else a legacy share (`$JAAH_SHARED_ACCOUNTS_ROOT` when set, else `LEGACY_SHARED_ACCOUNTS_ROOT`; a
       hand-configured share that predates NodeLinks) under the SAME gate: itself when it is an answering
       NFS mount, `""` while mountinfo lists a mount there that is not. An unmounted legacy path is never
       the store — a co-located node's daemon carries the override always, and its helper makes that
       directory root-owned 0750, where no login could write;
    3. else `{JAAH_HOME}/auth/.shared` as plain local disk — the account is then this node's alone.

    Says where the store is, not that it exists or is safe: `store_is_on_share` refuses a symlinked root."""
    nodelink = accounts_nodelink_root()
    if _is_accounts_nodelink_mount(nodelink):
        return nodelink
    if nodelink and _listed_as_mount(nodelink, no_table=False):
        return ""
    # Windows' twin of the line above: a link (or junction) at `.shared` that is not a mounted NodeLink is never
    # local disk, and nothing is read or minted through it.
    if nodelink and host_platform.is_windows() and _lexists_as_reparse_point(nodelink):
        return ""
    if nodelink and _is_macos() and os.path.islink(nodelink):
        return ""   # a link that is not root's to a live staging mount: neither the share nor local disk
    legacy = configured_shared_accounts_root()
    if legacy and _is_accounts_nodelink_mount(legacy):
        return legacy
    if legacy and _listed_as_mount(legacy, no_table=False):
        return ""
    return nodelink


# ── The file system mounted at the store root, OBSERVED (the node page's `nodelink` value) ───────────

# Where the file system id actually mounted at the store root is OBSERVED (`observed_efs_id`). Module
# constants so a test points them at fake trees; the root id pin is likewise a constant.
_PROC = "/proc"
_ROOT_ID = 0
# `mount.efs` names its stunnel/efs-proxy config `<state dir>/stunnel-config.<fs id>.<mount point with `/`
# as `.`>.<port>` (efs-utils `get_mount_specific_filename`) and execs one of these.
_PROXY_CONFIG = re.compile(r"^/var/run/efs/stunnel-config\.(fs-[0-9a-f]{8,40})\.(.+)\.(\d+)$")
_PROXY_BINARIES = frozenset({"stunnel", "stunnel4", "stunnel5", "efs-proxy"})
_DIRECT_SOURCE = re.compile(r"^(fs-[0-9a-f]{8,40})\.efs\.[a-z0-9-]+\.[a-z0-9.-]+:/")
_NFS_FSTYPES = frozenset({"nfs", "nfs4"})


def _mount_id(fd: int) -> str | None:
    """The mount id of the mount the open directory FD sits on (`/proc/self/fdinfo`), or `None`.

    Binds the observation to the mount the caller actually opened, not to whatever else a runner stacked at
    the same path."""
    try:
        for line in Path(_PROC, "self", "fdinfo", str(fd)).read_text().splitlines():
            if line.startswith("mnt_id:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _mount_of(mnt_id: str) -> tuple[str, str | None] | None:
    """`(source, port)` of the mountinfo entry with id MNT_ID when it is an NFS mount, else `None`.

    The fstype is required: a FUSE mount can carry any `fsname`, so only the kernel's NFS client's source is
    believed."""
    try:
        rows = list(_mountinfo_rows(os.path.join(_PROC, "self", "mountinfo")))   # `_PROC`: a test fakes the tree
    except OSError:
        return None
    for mnt, _, fstype, source, options in rows:
        if mnt != mnt_id:
            continue
        if fstype not in _NFS_FSTYPES:
            return None
        return source, next((o[5:] for o in options.split(",") if o.startswith("port=")), None)
    return None


def _read_at(dir_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        return os.read(fd, 1 << 16)
    finally:
        os.close(fd)


def _all_root(status: str, key: str) -> bool:
    """Every id on STATUS's `key:` line (real/effective/saved/fs) is `_ROOT_ID`."""
    for line in status.splitlines():
        if line.startswith(key + ":"):
            ids = line.split(":", 1)[1].split()
            return len(ids) == 4 and all(i == str(_ROOT_ID) for i in ids)
    return False


def _proxy_config_of(pid: str) -> list[str]:
    """The `/var/run/efs/stunnel-config…` arguments of PID if it is a root proxy process, else `[]`.

    One directory fd on `/proc/<pid>`, then `status` and `cmdline` relative to it, so both reads are of the
    same process (no PID reuse between them). Root is read from `status`, all four of uid and gid: the
    `/proc/<pid>` owner reads as root for any non-dumpable process, which a runner can arrange for its own."""
    try:
        proc_fd = os.open(Path(_PROC, pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return []
    try:
        argv = _read_at(proc_fd, "cmdline").split(b"\0")
        if os.path.basename(argv[0].decode("utf-8", "replace")) not in _PROXY_BINARIES:
            return []
        status = _read_at(proc_fd, "status").decode("utf-8", "replace")
        if not (_all_root(status, "Uid") and _all_root(status, "Gid")):
            return []
        return [arg.decode("utf-8", "replace") for arg in argv[1:]]
    except OSError:
        return []
    finally:
        os.close(proc_fd)


def observed_efs_id(root: str, root_fd: int) -> str | None:
    """The EFS file system id actually mounted at ROOT, OBSERVED from the system; `None` when it cannot be
    established (never a guess).

    The mount is the one ROOT_FD (the descriptor the caller holds on the root) sits on, found by its mount
    id, and must be an NFS mount (a FUSE mount can claim any source). A direct mount names the id in its
    source (`fs-xxx.efs.<region>.amazonaws.com:/`). An efs-utils TLS mount shows `127.0.0.1:/`, so the id comes
    from the root-owned stunnel/efs-proxy process `mount.efs` started for it (`_proxy_config_of`): its config
    path names the id, and is matched on the mount point AND on the `port=` the kernel recorded. Read from
    `/proc`, not `/var/run/efs` (mode 750, and a co-located node's daemon is not root). Two different ids, or
    any doubt, is `None`. Not tamper-evident against a process running as this daemon's own uid."""
    mnt_id = _mount_id(root_fd)
    mount = _mount_of(mnt_id) if mnt_id is not None else None
    if mount is None:
        return None
    source, port = mount
    direct = _DIRECT_SOURCE.match(source)
    if direct:
        return direct.group(1)
    if not source.startswith("127.0.0.1:") or port is None:
        return None
    dotted = os.path.realpath(root).replace("/", ".").lstrip(".")
    ids: set[str] = set()
    try:
        pids = [entry for entry in os.listdir(_PROC) if entry.isdigit()]
    except OSError:
        return None
    for pid in pids:
        for arg in _proxy_config_of(pid):
            match = _PROXY_CONFIG.match(arg)
            if match and match.group(2) == dotted and match.group(3) == port:
                ids.add(match.group(1))
    return next(iter(ids)) if len(ids) == 1 else None


# ── The node's NodeLink desired-state cache (written by the converge, read here) ─────────────────────

NODELINKS_CACHE_NAME = "nodelinks.json"
# What every runner is told about the NodeLinks mounted on its node (`fleet_dispatch.build_auth_lines`).
NODELINKS_ENV = "JAAH_NODELINKS"
#: Where a Windows node's NodeLink links point (`nodelink_windows.py`): the staging root the engine distro mounts
#: each access point under (`jaah-engine.sh`, `/run/jaah-nodelinks/<id>`), as the
#: runner account reaches that distro. Pinned to the script's literal by tests/test_nodelink_windows.py.
WINDOWS_NODELINK_TARGET_ROOT = "\\\\wsl.localhost\\jaah-engine\\run\\jaah-nodelinks"
_NODELINK_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
# A NodeLink id as the root helper keys its status file (`nodelink_mount._ID`).
_NODELINK_ID_RE = re.compile(r"[0-9a-f][0-9a-f-]{0,63}\Z")


def nodelinks_cache_path() -> str:
    """THE ONE resolver of the node-local NodeLink cache: `nodelinks.json` beside this node's `node.env`.

    A plain Linux node: `/etc/jaah/nodelinks.json` (`host_platform`'s etc dir). A co-located node: its
    `JAAH_NODE_ENV_FILE` is `layout.node_env(v)`, so this is `layout.etc_dir(v)/nodelinks.json` — where the
    host helper writes that vnode's cache. Sandbox: beside the sandbox's own `node.env`. Windows: in the
    daemon's state directory, since the daemon writes it itself (`nodelink_windows`) and `etc` is not its to
    write there."""
    if host_platform.is_windows():
        return ntpath.join(host_platform.paths().state_dir, NODELINKS_CACHE_NAME)
    return os.path.join(os.path.dirname(environment.node_env_path()), NODELINKS_CACHE_NAME)


def _valid_nodelink(entry: object) -> dict[str, str] | None:
    """One cache entry re-validated (the cache is CP data), or None: `slug` `[a-z0-9][a-z0-9-]{0,62}`, `path`
    absolute with no `..` and no control character. The name falls back to the slug; a name carrying a control
    character refuses the entry, since it is exported to a runner. Any other key (an old cache's `kind`, a
    per-entry `store`) is the mount helper's business and ignored here."""
    if not isinstance(entry, dict):
        return None
    slug, path = entry.get("slug"), entry.get("path")
    name = entry.get("name") if entry.get("name") is not None else slug
    if not isinstance(slug, str) or not _NODELINK_SLUG_RE.match(slug):
        return None
    if host_platform.is_windows():
        path = windows_absolute(path) if isinstance(path, str) else ""
        if not path:
            return None
    elif (not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/")
            or _has_control_character(path) or not path.rstrip("/")):
        return None
    if not isinstance(name, str) or _has_control_character(name):
        return None
    return {"id": str(entry.get("id") or ""), "slug": slug, "name": name.strip() or slug,
            "path": path.rstrip("/")}


def read_nodelinks() -> list[dict[str, str]]:
    """The cached NodeLinks, each re-validated; a malformed entry is skipped. A missing, unreadable or
    garbled cache is "no NodeLinks" (`[]`), never an error: nothing here may fail a launch."""
    entries = file_io.read_json_or(nodelinks_cache_path(), {}, expect=dict).get("nodelinks")
    if not isinstance(entries, list):
        return []
    return [valid for valid in map(_valid_nodelink, entries) if valid is not None]


# What the root helper made of each NodeLink's mount (`nodelink_mount.py`), beside the cache.
NODELINKS_STATUS_NAME = "nodelinks.status.json"
NODELINK_STATES = ("mounted", "not_mounted", "unsafe_path", "unreachable", "helper_missing", "needs_permission")


def nodelinks_status_path() -> str:
    """`nodelinks.status.json` beside the cache (`nodelinks_cache_path`): the helper's per-NodeLink record (on
    Windows, the daemon's own converger's)."""
    paths = ntpath if host_platform.is_windows() else os.path
    return paths.join(paths.dirname(nodelinks_cache_path()), NODELINKS_STATUS_NAME)


def nodelink_mount_states() -> dict[str, dict[str, str]]:
    """The config snapshot's `nodelinks` value (Architecture §4.2): `{id: {state, path, detail}}`.

    Every DESIRED NodeLink (the cache) is reported; its state is `mounted` only when its path is a mount RIGHT
    NOW (`ismount`), else the helper's recorded failure for that same path, else `not_mounted`. A NodeLink no
    longer desired is OMITTED once its path is unmounted — the CP's two-phase delete waits on that — and is
    reported `mounted` while it still is. A macOS `needs_permission` (TCC refused the helper's read of a share that
    IS mounted) is published as recorded, mounted or not: the share is up but unusable until the grant. With no status file (a co-located node whose host has not written
    one) the answer is `ismount` alone. Blocks on a hung NFS server: call it off the event loop."""
    records = file_io.read_json_or(nodelinks_status_path(), {}, expect=dict)
    out: dict[str, dict[str, str]] = {}
    for link in read_nodelinks():
        ident, path = link["id"], link["path"]
        if not ident or ident in out:
            continue
        got = records.get(ident)
        rec: dict = got if isinstance(got, dict) else {}
        if rec.get("path") == path and rec.get("state") == "needs_permission":
            out[ident] = {"state": "needs_permission", "path": path, "detail": _one_line(rec.get("detail"))}
            continue
        if _nodelink_mounted(path, ident):
            out[ident] = {"state": "mounted", "path": path, "detail": ""}
            continue
        state = rec.get("state")
        failed = rec.get("path") == path and state in NODELINK_STATES and state != "mounted"
        out[ident] = {"state": state if failed else "not_mounted", "path": path,
                      "detail": _one_line(rec.get("detail")) if failed else ""}
    for ident, rec in records.items():
        if ident in out or not isinstance(rec, dict) or not isinstance(rec.get("path"), str):
            continue
        if not _NODELINK_ID_RE.match(ident) or not (
                windows_absolute(rec["path"]) if host_platform.is_windows() else rec["path"].startswith("/")):
            continue
        if rec.get("state") in ("mounted", "needs_permission") and _nodelink_mounted(rec["path"], ident):
            out[ident] = {"state": "mounted", "path": _one_line(rec["path"]), "detail": "not in this node's list; still mounted"}
    return out


def _one_line(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in " ".join(text.split()))[:300]


# ── Windows: the NodeLink links (made by `nodelink_windows.py`, read here) ─────────────────────────────

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def windows_link_target(ident: str) -> str:
    """The exact target of NodeLink IDENT's link on a Windows node."""
    return f"{WINDOWS_NODELINK_TARGET_ROOT}\\{ident}"


def is_reparse_point(info: os.stat_result) -> bool:
    """A symlink or, on Windows, any reparse point — a junction, which `lstat` reports as a directory."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _lexists_as_reparse_point(path: str) -> bool:
    try:
        return is_reparse_point(os.lstat(path))
    except (OSError, ValueError):
        return False


def link_target_of(path: str) -> str | None:
    """What the symlink at PATH names, in plain UNC or drive form (the kernel's `\\\\?\\UNC\\` and `\\??\\`
    prefixes dropped), or None when PATH is not a symlink. Reads the link itself, never through it."""
    try:
        if not os.path.islink(path):
            return None
        raw = os.readlink(path)
    except (OSError, ValueError):
        return None
    for prefix, spelled in (("\\\\?\\UNC\\", "\\\\"), ("\\??\\UNC\\", "\\\\"), ("\\\\?\\", ""), ("\\??\\", "")):
        if raw.startswith(prefix):
            return spelled + raw[len(prefix):]
    return raw


def is_own_windows_link(path: str, ident: str) -> bool:
    """Is PATH a symlink naming exactly NodeLink IDENT's target (`windows_link_target`, case-insensitively)?"""
    target = link_target_of(path)
    return target is not None and ntpath.normcase(target) == ntpath.normcase(windows_link_target(ident))


def windows_status_records() -> dict[str, dict]:
    """The Windows converger's status file as `{id: record}`, each id a NodeLink id and each record carrying a
    drive-absolute `path`; `{}` when absent or garbled."""
    raw = file_io.read_json_or(nodelinks_status_path(), {}, expect=dict)
    return {ident: rec for ident, rec in raw.items()
            if isinstance(ident, str) and _NODELINK_ID_RE.match(ident) and isinstance(rec, dict)
            and isinstance(rec.get("path"), str) and windows_absolute(rec["path"])}


def _windows_mounted_id(path: str) -> str:
    """The id of the NodeLink mounted at PATH on a Windows node — its status record names PATH `mounted` AND
    the link there names exactly that id's target — else `""`."""
    want = ntpath.normcase(path or "")
    if not want:
        return ""
    for ident, rec in windows_status_records().items():
        if rec.get("state") == "mounted" and ntpath.normcase(rec["path"]) == want and is_own_windows_link(path, ident):
            return ident
    return ""


def _windows_links() -> list[str]:
    """Every recorded NodeLink path whose link is still this node's own, mounted or not."""
    return [rec["path"] for ident, rec in windows_status_records().items() if is_own_windows_link(rec["path"], ident)]


def _under_windows_nodelink(path: str) -> bool:
    """Is PATH the Windows token store root while it is a mounted NodeLink link, or lexically below it?"""
    root = account_token_root()
    if not root or not _windows_mounted_id(root):
        return False
    base, want = ntpath.normcase(root), ntpath.normcase(path)
    return want == base or (want.startswith(base + "\\") and ".." not in want.split("\\"))


def recorded_efs_id(path: str) -> str | None:
    """The file system of the NodeLink mounted at PATH on a Windows node, as its status record names it (this
    daemon mounted it, through the engine distro), or None."""
    ident = _windows_mounted_id(path)
    efs_id = windows_status_records().get(ident, {}).get("efs_id") if ident else None
    return efs_id if isinstance(efs_id, str) and re.fullmatch(r"fs-[0-9a-f]{8,40}", efs_id) else None


def mounted_nodelinks() -> list[dict[str, str]]:
    """`{name, slug, path}` of every cached NodeLink whose path is a MOUNT right now, in cache order, one per
    path. Listed-but-unmounted is not advertised: a runner relies on this as truth."""
    out, seen = [], set()
    for link in read_nodelinks():
        path = link["path"]
        if path in seen or not _nodelink_mounted(path, link["id"]):
            continue
        seen.add(path)
        out.append({"name": link["name"], "slug": link["slug"], "path": path})
    return out


def nodelinks_env_value() -> str:
    """`$JAAH_NODELINKS`: the compact JSON array of `mounted_nodelinks()`, `""` when there are none."""
    links = mounted_nodelinks()
    return json.dumps(links, separators=(",", ":"), ensure_ascii=False) if links else ""


def resolve_shared_auth_dir(slug: str | None) -> str:
    """`<account_token_root()>/<slug>` — where this account's token is minted and, failing a credential in
    its config dir, read — or `""`.

    Node-independent by construction: no home argument, ever. Fails closed to `""` on a slug that is not
    one path segment, and on a control character (a copy of the CP's
    `app/shared_storage.py::has_control_character`, never imported) — the value becomes an env var."""
    slug = (slug or "").strip().strip("/")
    if not slug or "/" in slug or slug in (".", "..") or _has_control_character(slug):
        return ""
    windows = host_platform.is_windows()
    if windows and ("\\" in slug or ":" in slug):
        return ""
    root = account_token_root()
    if not root:
        return ""
    return f"{root}\\{slug}" if windows else f"{root}/{slug}"


def account_slug(config_dir: object) -> str:
    """The account SLUG a credential config dir names — its last path component, `""` when unresolvable.

    Node-side twin of the CP's `account_slug_from_dir`. Refuses a non-absolute path and a `..` segment:
    `normpath("<home>/auth/acct1/..")` is `auth`, a legal slug — another account's store."""
    stripped = str(config_dir or "").strip()
    if host_platform.is_windows():
        # A Windows config dir (`C:\...\auth\<slug>`), under the same refusals. Either separator: the daemon
        # joins `<home>/auth/<slug>` in places, and an empty slug here hid the account's store from dispatch,
        # which then refused a signed-in account (a real node).
        path = windows_absolute(stripped.replace("/", "\\"))
        return ntpath.basename(path) if path else ""
    if not stripped or not stripped.startswith("/") or ".." in stripped.split("/"):
        return ""
    return os.path.basename(os.path.normpath(stripped))


def credential_file_present(directory: str, only: str = "") -> bool:
    """Is there a credential file (`_credential_files`) in DIRECTORY — with UNREADABLE answered True.

    Presence only; the token-shape ladder is the caller's. Unreadable is present, or a real store under an
    untraversable dir would vanish. `lstat`, and a SYMLINK is not present: the adapters refuse a symlinked
    credential, and a `shared_token_link` account's per-node file is a link into the store, which is no
    credential of the home's own. `only` asks about that one relative name instead of every provider's.
    Under a Windows NodeLink link every failure is unreadable: this daemon cannot read through it."""
    through_link = host_platform.is_windows() and _under_windows_nodelink(directory)
    for name in ((only,) if only else _credential_files()):
        try:
            info = os.lstat(os.path.join(directory, name))
        except PermissionError:
            return True
        except OSError:
            if through_link:
                return True
            continue
        if not stat.S_ISLNK(info.st_mode):
            return True
    return False


def shared_token_dir(config_dir: object) -> str:
    """This account's token store `<root>/<slug>` when it holds a credential and is safe to read, else `""`.

    Safe: the root and every directory down to the credential are real directories, never symlinks
    (`store_is_on_share`). Mounted or not is not asked: an unmounted store is this node's own."""
    store = resolve_shared_auth_dir(account_slug(config_dir))
    if not store or not store_is_on_share(store):
        return ""
    present = any(credential_file_present(store, name) for name in _credential_files()
                  if store_is_on_share(store, name))
    return store if present else ""


def _root_info(root: str) -> os.stat_result | None:
    """`lstat` of the store root when it is a real directory (never a symlink, nor a junction), else None. macOS:
    root's link to its staging mount stands for that mount's root (`_darwin_staging_target`)."""
    target = _token_share_target(root)
    try:
        info = os.stat(target) if target else os.lstat(root)
    except OSError:
        return None
    return info if stat.S_ISDIR(info.st_mode) and not is_reparse_point(info) else None


def store_is_on_share(store: str, relative: str = "") -> bool:
    """Is the store root a real directory, and STORE (one account's `<root>/<slug>`) — and every directory
    between it and RELATIVE's file — absent, or a REAL directory on the root's own filesystem?

    `auth/` is group-writable and a mounted store is writable by every mounting node, so any of them can plant
    `.shared`, `<root>/<slug>` or a directory below it as a symlink to local disk or to another account's
    store — a login would then mint a credential off the store, and a runner would read another account's.
    `lstat`, so a link is refused, never followed; each directory's device must be the root's. Absent is True
    (nothing below it exists yet; a login creates it); an absent ROOT is False (`ensure_shared_root` makes
    it). Point-in-time on a multi-writer filesystem: the writers are the one trust domain § Accepted limits
    names. Any other `OSError` is False.

    Windows, with the root a mounted NodeLink link: lexical only — STORE at or below the root, RELATIVE
    with no `..` — since this daemon cannot read through the link (the module docstring)."""
    root = account_token_root()
    if host_platform.is_windows() and root and _windows_mounted_id(root):
        rel = PureWindowsPath(relative)
        return _under_windows_nodelink(store) and ".." not in rel.parts and not (rel.drive or rel.root)
    root_info = _root_info(root)
    if root_info is None:
        return False
    path = store
    for part in ("",) + Path(relative).parent.parts if relative else ("",):
        path = os.path.join(path, part) if part else path
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode) or is_reparse_point(info) or info.st_dev != root_info.st_dev:
            return False
    return True


# The store root's and each account leaf's mode: setgid so the daemon and the session user share it (as an
# account config dir, `accounts.ACCOUNT_DIR_MODE`); inert through an access point, which forces uid 3000.
STORE_DIR_MODE = 0o2770


def ensure_shared_root() -> str:
    """Create the token store root `{JAAH_HOME}/auth/.shared` if it is missing, at `STORE_DIR_MODE` and owned
    like `{JAAH_HOME}/auth`. `""` when the root is now a real directory, else a one-line reason (a symlink or
    a non-directory there is refused, never replaced)."""
    root = account_token_root()
    if not root:
        return "this node has no token store it can use (no JAAH_HOME, or a store mount that is not answering)"
    if host_platform.is_windows() and _windows_mounted_id(root):
        return ""   # a mounted NodeLink link: the share's root, never this daemon's to create
    if host_platform.is_windows() and root == accounts_nodelink_root():
        # The runner account creates it, as everything under its home; `mkdir -p` is idempotent.
        import session_user  # noqa: PLC0415 — deferred: this module is a hook-loaded leaf

        failure = session_user.ensure_dir_as_session_user(root)
        if failure:
            return f"could not create {root} ({failure})"
    elif root == accounts_nodelink_root():
        try:
            os.mkdir(root, STORE_DIR_MODE)
        except FileExistsError:
            pass
        except OSError as exc:
            return f"could not create {root} ({type(exc).__name__})"
        else:
            try:
                os.chmod(root, STORE_DIR_MODE, follow_symlinks=False)
                parent = os.lstat(os.path.dirname(root))
                os.chown(root, parent.st_uid, parent.st_gid, follow_symlinks=False)
            except (OSError, NotImplementedError) as exc:
                log.debug("could not match %s's owner/mode to its parent: %s", root, exc)
    if _root_info(root) is None:
        return f"{root} is not a real directory (a symlink or a file sits there); refusing to use it"
    return ""


# The largest credential file a publish copies; the vendors' are a few KiB.
_MAX_CREDENTIAL_BYTES = 1024 * 1024


def publish_credential(src: str, store: str, relative: str) -> str:
    """Copy the credential file SRC (a node-local login's regular file) to `<store>/<relative>` in the token
    store, atomically. `""` on success, else a one-line reason — never the file's contents.

    For a `shared_token_link` login: the CLI mints in its own per-node home, and only this one file crosses.
    Every hop is no-follow: SRC's last component, the store and each parent under it (opened
    `O_DIRECTORY|O_NOFOLLOW` and walked by descriptor, created if absent), and the temp file (`O_EXCL`); each
    directory must sit on the store root's device. Run as the uid that can read SRC — the session user on a
    uid-split host (`python3 shared_storage.py --publish-credential SRC STORE RELATIVE`). On Windows the
    daemon runs it, and the copy is the runner account's (`_publish_credential_windows`)."""
    if not store_is_on_share(store, relative):
        return f"{store} is not a real directory in the token store"
    if host_platform.is_windows():
        return _publish_credential_windows(src, store, relative)
    try:
        root_info = _root_info(account_token_root())
        if root_info is None:
            return f"{store} is not a real directory in the token store"
        root_dev = root_info.st_dev
        sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(sfd).st_mode):
                return f"{src} is not a regular file"
            data = os.read(sfd, _MAX_CREDENTIAL_BYTES + 1)
        finally:
            os.close(sfd)
        if not data or len(data) > _MAX_CREDENTIAL_BYTES:
            return f"{src} is empty or larger than {_MAX_CREDENTIAL_BYTES} bytes"
        os.makedirs(store, mode=STORE_DIR_MODE, exist_ok=True)
        dfd = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in Path(relative).parent.parts:
                try:
                    os.mkdir(part, STORE_DIR_MODE, dir_fd=dfd)
                except FileExistsError:
                    pass
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
                os.close(dfd)
                dfd = nfd
            if os.fstat(dfd).st_dev != root_dev:
                return f"{store} is not on the token store's filesystem"
            name = Path(relative).name
            staged = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            tfd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                os.write(tfd, data)
                os.fsync(tfd)
            finally:
                os.close(tfd)
            os.replace(staged, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        return f"could not publish the credential to {store} ({type(exc).__name__})"
    return ""


# Run AS THE RUNNER ACCOUNT under Git for Windows' MSYS `sh`: copy beside the destination, then rename over it,
# so a reader sees the old credential or the new one. Paths only, on the env; the temp file never outlives a
# failure.
_WINDOWS_PUBLISH_SH = ('mkdir -p -- "$SU_DIR" && cp -- "$SU_SRC" "$SU_TMP" && chmod 600 -- "$SU_TMP" '
                       '&& mv -f -- "$SU_TMP" "$SU_DEST" || { rm -f -- "$SU_TMP"; exit 1; }')


def _publish_credential_windows(src: str, store: str, relative: str) -> str:
    """`publish_credential` on a Windows node: the copy runs as the runner account through the session-user
    seam. The daemon could not reach a store under a mounted NodeLink link (`\\\\wsl.localhost` is per-user),
    and it writes nothing under the runner's home itself. There is no descriptor walk there (no
    `O_NOFOLLOW`/`O_DIRECTORY`/`dir_fd`), so SRC is `lstat`'d by path — a regular file, not a reparse point,
    within the size cap — and the store side is `store_is_on_share`'s, as the caller asked it. A path walk,
    as `fleet_dispatch._link_credential_by_path`'s: a hop swapped after its check is not excluded."""
    import session_user  # noqa: PLC0415 — deferred: this module is a hook-loaded leaf

    try:
        info = os.lstat(src)
    except OSError as exc:
        return f"{src} is not a file this node can open ({type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode) or is_reparse_point(info):
        return f"{src} is not a regular file"
    if not info.st_size or info.st_size > _MAX_CREDENTIAL_BYTES:
        return f"{src} is empty or larger than {_MAX_CREDENTIAL_BYTES} bytes"
    dest = ntpath.join(store, *PureWindowsPath(relative).parts)
    directory, name = ntpath.split(dest)
    staged = ntpath.join(directory, f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    shell = session_user._shell_path   # the seam's MSYS spelling of each path, as `guard_bundle` uses it
    try:
        proc = session_user.run_as_session_user(
            ["/bin/sh", "-c", _WINDOWS_PUBLISH_SH],
            env={"SU_SRC": shell(src), "SU_DIR": shell(directory), "SU_TMP": shell(staged), "SU_DEST": shell(dest)},
            timeout=60)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (seam down), TimeoutExpired, OSError
        return f"could not publish the credential to {store} (the runner account's copy could not run: " \
               f"{type(exc).__name__})"
    if proc.returncode != 0:
        return f"could not publish the credential to {store} ({_one_line(proc.stderr) or f'rc {proc.returncode}'})"
    return ""


def share_mounted() -> bool:
    """Is the token store a MOUNT right now (a NodeLink, or a legacy share)? For what a reader is told and for
    what a removal may touch — never a precondition of reading or minting a token."""
    root = account_token_root()
    return bool(root) and _nodelink_mounted(root, _nodelink_id_at(root) if _is_macos() else "")


def token_dir(config_dir: str, store: str = "") -> str:
    """THE ONE RESOLVER: which directory holds THIS account's token file.

    Precedence:
      1. an explicit `store` wins outright — only a login names one (the store its own env names, which
         legitimately holds no token yet while it polls);
      2. the token store `<root>/<slug>`, if it holds a credential and is safe to read (`shared_token_dir`)
         — ahead of the config dir, whose credential may be one left behind when the token moved;
      3. the config dir — where an account minted before every login went to the store keeps its token,
         and where a "no credential" message points.

    Says where to ask, not that a usable token is there."""
    if store:
        return store
    return shared_token_dir(config_dir) or config_dir


#: `token_source`'s two answers, the Brains page's "Shared"/"Local" (`account_observations.extra["token_source"]`).
#: The pair of the CP's `account_fold.TOKEN_SOURCES`, which reads them.
TOKEN_SOURCE_SHARED = "shared"
TOKEN_SOURCE_LOCAL = "local"


def token_source(config_dir: str, *, mounted: bool | None = None) -> str:
    """Where `token_dir` finds this account's token, in one word: `"shared"` when it resolves into the token
    store AND that store is a mount right now (`share_mounted`; `mounted` passes one pass's answer in), else
    `"local"` — an unmounted `.shared` is this node's own disk. Reports the resolver; decides nothing."""
    if token_dir(config_dir) == config_dir:
        return TOKEN_SOURCE_LOCAL
    return TOKEN_SOURCE_SHARED if (share_mounted() if mounted is None else mounted) else TOKEN_SOURCE_LOCAL


def securestorage_dir(config_dir: str) -> str:
    """The value of `CLAUDE_SECURESTORAGE_CONFIG_DIR` for this account: its token store when `token_dir`
    resolves there (the store holds a credential), else `""` — the variable is then OMITTED, never exported empty (the CLI reads an empty
    one as `~/.claude`) and never set to the config dir."""
    found = token_dir(config_dir)
    return "" if found == config_dir else found


def probe_env(config_dir: str) -> dict[str, str]:
    """The account pin every daemon-side `claude` probe needs — `CLAUDE_CONFIG_DIR` plus, when the token is
    in the store, `CLAUDE_SECURESTORAGE_CONFIG_DIR` naming it (`securestorage_dir`). Without that pin the
    probe sees an empty store, reads "no live session", and the CP hands the slot away."""
    env = {"CLAUDE_CONFIG_DIR": config_dir}
    store = securestorage_dir(config_dir)
    if store:
        env[SECURESTORAGE_ENV] = store
    return env


def credential_home(config_dir: str, provider: object) -> str:
    """Where this account's credential file PHYSICALLY lives, laid out as a CLI home: the token store for a
    `shared_token_link` provider whose credential is there (`<store>/<credential_file>`), else `config_dir`.

    For credential questions only — the probe that reads it (store first, as `token_dir`). A runner's CLI home is ALWAYS `config_dir`:
    running a CLI in the store would execute config any mounting node can write. Claude and an unknown
    provider keep `config_dir` (Claude's store is named by `securestorage_dir`). Lazy import: this module
    is a hook-loaded leaf."""
    import agent_providers  # noqa: PLC0415 — deferred, see the docstring

    row = agent_providers.by_value(str(provider or "") or agent_providers.DEFAULT_PROVIDER)
    if row is not None and row.shared_token_link:
        return token_dir(config_dir)
    return config_dir


if __name__ == "__main__":
    # The session-user leg of `publish_credential` (a uid-split host's login writes its credential 0600 as that
    # user). Argv carries paths only; the reason, never the contents, goes to stderr.
    if len(sys.argv) == 5 and sys.argv[1] == "--publish-credential":
        reason = publish_credential(sys.argv[2], sys.argv[3], sys.argv[4])
        if reason:
            print(reason, file=sys.stderr)
        sys.exit(1 if reason else 0)
    print("usage: shared_storage.py --publish-credential <src> <store> <relative>", file=sys.stderr)
    sys.exit(2)
