"""macOS (Apple Silicon): the seam answered by the Darwin kernel — `LOCAL_PEERPID` + `getpeereid` for
who is asking, `libproc`'s `proc_pidinfo` for the ancestry walk, mach `host_statistics` for CPU, and
launchd for supervision. No procfs exists here, so nothing below reads a file to learn about a process.

Layout (Architecture §4.2): the bundle under `/Library/Jaah` (SIP keeps `/usr/lib` read-only), config in
`/Library/Jaah/etc/node.env`, the runtime tree under `/var/run/jaah`, the session user's jaah home at
`/var/jaah`, and the node-managed engine's socket at `/var/run/jaah/docker.sock`.

Every native call goes through one module-level hook (`_proc_pidinfo`, `_getpeereid`, `_host_cpu_ticks`,
`_vm_pages`, `_sysctl_u64`, `_procargs`, `_kinfo_start`, `_run`) so `tests/test_host_platform_macos.py` drives this module on Linux with fakes."""
from __future__ import annotations

import ctypes
import functools
import ipaddress
import json
import logging
import os
import platform as _platform
import subprocess
import sys

from host_platform import _posix
from host_platform._types import absent, Paths, Peer, Service, metrics_sample

OS = "macos"

_PATHS = Paths(
    etc_dir="/Library/Jaah/etc",
    run_dir="/var/run/jaah",
    creds_socket="/var/run/jaah/creds/sock",
    state_dir="/Library/Jaah/state",
    bundle_root="/Library/Jaah",
    lib_root="/Library/Jaah/current",
    python="/Library/Jaah/current/runtime/python/bin/python3",
    home_root="/var/jaah",
    docker_host="unix:///var/run/jaah/docker.sock",
    tmux="/var/run/jaah/tmux",
    # Each LaunchDaemon's StandardOutPath/StandardErrorPath (the packaged launchd plists).
    log_dir="/Library/Jaah/log",
)

# The installer's hidden service users (`apps/api/app/node_bootstrap.py`'s `MACOS_RUNNER_USER` /
# `MACOS_DAEMON_USER`): runners and console sessions run as the first, the daemon as the second.
RUNNER_ACCOUNT = "_jaah"
DAEMON_ACCOUNT = "_jaahd"

log = logging.getLogger("jaahd.host_platform.macos")

lock = _posix.lock
unlock = _posix.unlock
serve_socket = _posix.serve_socket
connect_socket = _posix.connect_socket
pane_os_pid = _posix.pane_os_pid
# The root links macOS keeps for its BSD layout. The kernel reports every cwd through the target, so a slot
# the daemon spells `/var/jaah/repos/…` is `/private/var/jaah/repos/…` to `claude agents`, a tmux pane and
# `F_GETPATH`. Folding the link into the key is still a string rule: these links are fixed by the OS.
_PRIVATE_LINKS = ("/var", "/tmp", "/etc")


def path_key(path: str) -> str:
    """`_posix.path_key`, with a root link (`/var`, `/tmp`, `/etc`) spelled as its `/private` target."""
    key = _posix.path_key(path)
    for link in _PRIVATE_LINKS:
        if key == link or key.startswith(link + "/"):
            return "/private" + key
    return key


def is_under(path: str, root: str) -> bool:
    """`_posix.is_under` over `path_key`'s spelling, so either side of a root link compares equal."""
    return _posix.is_under(path_key(path), path_key(root))

# <sys/un.h>: the local-socket option level and its peer-pid option.
SOL_LOCAL = 0
LOCAL_PEERPID = 0x002
# <sys/fcntl.h>: the descriptor's path, into a MAXPATHLEN buffer.
F_GETPATH = 50
MAXPATHLEN = 1024
# <sys/proc_info.h>: the BSD info flavour of `proc_pidinfo`, and its short form (`_short_bsdinfo`).
PROC_PIDTBSDINFO = 3
PROC_PIDT_SHORTBSDINFO = 13
MAXCOMLEN = 16


def machine() -> str:
    return _platform.machine()


def os_version() -> str:
    """The product version (`14.5`), not the Darwin kernel release — what an operator recognises."""
    return _platform.mac_ver()[0] or _platform.release()


def paths() -> Paths:
    return _PATHS


# Unknown: `systemd-detect-virt` is Linux's, and nothing here answers it as cheaply.
virtualization = absent


# The Virtualization.framework helper (`apps/jaahd/vz/`, built and signed on the macOS CI job), the bundle's
# `libexec/jaah-vz`. Its `probe` answers this node's three hypervisor facts (Architecture §9.1).
VZ_HELPER = _PATHS.lib_root + "/libexec/jaah-vz"
VZ_PROBE_TIMEOUT_S = 10


@functools.cache
def _vz_probe() -> tuple[bool, str | None]:
    """`(usable, microvm_prereq)` from one `jaah-vz probe`, asked once: the helper is replaced only by an
    update, which restarts the daemon. `vz_unsupported` when the Mac cannot run Virtualization.framework;
    `vz_helper` for every helper fault — absent, unsigned for the entitlement, a non-zero exit, a hang, or
    output that is not the pinned `{"usable": bool, "error": null|"unsupported"|"entitlement"}`."""
    try:
        proc = _run([VZ_HELPER, "probe"], capture_output=True, text=True, timeout=VZ_PROBE_TIMEOUT_S,
                    check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("jaah-vz probe could not run: %s", exc)
        return False, "vz_helper"
    if proc.returncode != 0:
        log.info("jaah-vz probe exited %s: %s", proc.returncode, (proc.stderr or "").strip()[:200])
        return False, "vz_helper"
    try:
        answer = json.loads(proc.stdout)
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("usable"), bool):
        log.info("jaah-vz probe answered %r", (proc.stdout or "")[:200])
        return False, "vz_helper"
    if answer["usable"]:
        return True, None
    return False, "vz_unsupported" if answer.get("error") == "unsupported" else "vz_helper"


def kvm() -> bool:
    """Whether this Mac can run a MicroVM: `jaah-vz probe`'s `usable` (the field keeps its Linux name)."""
    return _vz_probe()[0]


def hypervisor() -> str:
    return "vz"


def microvm_prereq() -> str | None:
    """The unmet MicroVM prerequisite (`vz_unsupported` | `vz_helper`), None when the probe is usable."""
    return _vz_probe()[1]


# ── native bindings, loaded on first use (never at import: this module imports on Linux in tests) ─────

class ProcBsdInfo(ctypes.Structure):
    """`struct proc_bsdinfo` (<sys/proc_info.h>), 136 bytes on arm64."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class ProcBsdShortInfo(ctypes.Structure):
    """`struct proc_bsdshortinfo` (<sys/proc_info.h>), 64 bytes."""

    _fields_ = [
        ("pbsi_pid", ctypes.c_uint32),
        ("pbsi_ppid", ctypes.c_uint32),
        ("pbsi_pgid", ctypes.c_uint32),
        ("pbsi_status", ctypes.c_uint32),
        ("pbsi_comm", ctypes.c_char * MAXCOMLEN),
        ("pbsi_flags", ctypes.c_uint32),
        ("pbsi_uid", ctypes.c_uint32),
        ("pbsi_gid", ctypes.c_uint32),
        ("pbsi_ruid", ctypes.c_uint32),
        ("pbsi_rgid", ctypes.c_uint32),
        ("pbsi_svuid", ctypes.c_uint32),
        ("pbsi_svgid", ctypes.c_uint32),
        ("pbsi_rfu", ctypes.c_uint32),
    ]


class VmStatistics64(ctypes.Structure):
    """`struct vm_statistics64` (<mach/vm_statistics.h>), 152 bytes, 8-aligned."""

    _fields_ = [
        ("free_count", ctypes.c_uint32), ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32), ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64), ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64), ("pageouts", ctypes.c_uint64), ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64), ("lookups", ctypes.c_uint64), ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", ctypes.c_uint32), ("speculative_count", ctypes.c_uint32),
        ("decompressions", ctypes.c_uint64), ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64), ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", ctypes.c_uint32), ("throttled_count", ctypes.c_uint32),
        ("external_page_count", ctypes.c_uint32), ("internal_page_count", ctypes.c_uint32),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


# mach: the host statistics flavours read here, and their CPU tick order.
HOST_CPU_LOAD_INFO = 3
HOST_VM_INFO64 = 4
CPU_STATE_USER, CPU_STATE_SYSTEM, CPU_STATE_IDLE, CPU_STATE_NICE = 0, 1, 2, 3
# <sys/sysctl.h>: a process's argument area.
CTL_KERN = 1
KERN_ARGMAX = 8
KERN_PROCARGS2 = 49
# `struct kinfo_proc` for one pid; its first field is `kp_proc.p_starttime` (a timeval).
KERN_PROC = 14
KERN_PROC_PID = 1
KINFO_PROC_SIZE = 648

# libSystem carries libc, libproc and the mach calls alike; resolved from the dyld shared cache.
_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
_NATIVE: dict = {}


def _libsystem():
    """The loaded libSystem with every function this module calls typed — once per process."""
    if "lib" not in _NATIVE:
        lib = ctypes.CDLL(_LIBSYSTEM, use_errno=True)
        c_int, c_uint, c_size_p = ctypes.c_int, ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)
        lib.proc_pidinfo.argtypes = [c_int, c_int, ctypes.c_uint64, ctypes.c_void_p, c_int]
        lib.proc_pidinfo.restype = c_int
        lib.getpeereid.argtypes = [c_int, ctypes.POINTER(c_uint), ctypes.POINTER(c_uint)]
        lib.getpeereid.restype = c_int
        lib.mach_host_self.argtypes = []
        lib.mach_host_self.restype = c_uint
        for name in ("host_statistics", "host_statistics64"):
            fn = getattr(lib, name)
            fn.argtypes = [c_uint, c_int, ctypes.c_void_p, ctypes.POINTER(c_uint)]
            fn.restype = c_int
        lib.sysctl.argtypes = [ctypes.POINTER(c_int), ctypes.c_uint, ctypes.c_void_p, c_size_p,
                               ctypes.c_void_p, ctypes.c_size_t]
        lib.sysctl.restype = c_int
        lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, c_size_p, ctypes.c_void_p,
                                     ctypes.c_size_t]
        lib.sysctlbyname.restype = c_int
        _NATIVE["lib"] = lib
        # One send right for the process's life: `mach_host_self()` adds a reference on every call.
        _NATIVE["host"] = lib.mach_host_self()
    return _NATIVE["lib"]


def _native_proc_pidinfo(pid: int, flavor: int, buf) -> int:
    """`proc_pidinfo(pid, flavor, 0, buf, sizeof buf)` → bytes written (<= 0 on failure)."""
    return int(_libsystem().proc_pidinfo(int(pid), int(flavor), 0, ctypes.byref(buf), ctypes.sizeof(buf)))


def _native_getpeereid(fd: int) -> tuple[int, int]:
    """`getpeereid(fd, &uid, &gid)` → (uid, gid); raises `OSError` on failure."""
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    if _libsystem().getpeereid(int(fd), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return uid.value, gid.value


def _native_host_cpu_ticks() -> tuple | None:
    """Cumulative `(user, system, idle, nice)` ticks across every core, or None."""
    lib = _libsystem()
    ticks = (ctypes.c_uint32 * 4)()
    count = ctypes.c_uint32(4)
    if lib.host_statistics(_NATIVE["host"], HOST_CPU_LOAD_INFO, ctypes.byref(ticks), ctypes.byref(count)):
        return None
    return tuple(int(t) for t in ticks)


def _native_vm_pages() -> dict | None:
    """`host_statistics64(HOST_VM_INFO64)`'s page counts, by field name, or None."""
    lib = _libsystem()
    vm = VmStatistics64()
    count = ctypes.c_uint32(ctypes.sizeof(vm) // 4)
    if lib.host_statistics64(_NATIVE["host"], HOST_VM_INFO64, ctypes.byref(vm), ctypes.byref(count)):
        return None
    return {name: int(getattr(vm, name)) for name, _type in VmStatistics64._fields_}


def _native_sysctl_u64(name: str) -> int | None:
    """An integer `sysctlbyname` value (`hw.memsize`, `hw.pagesize`), or None."""
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if _libsystem().sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0):
        return None
    return int(value.value)


def _native_procargs(pid: int) -> bytes | None:
    """The raw `KERN_PROCARGS2` area for `pid`, or None (gone, or not ours to read)."""
    lib = _libsystem()
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    mib = (ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX)
    if lib.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
        return None
    buf = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    mib3 = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, int(pid))
    if lib.sysctl(mib3, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[:size.value]


def _native_kinfo_start(pid: int) -> tuple[int, int] | None:
    """`pid`'s start time `(sec, usec)` from `sysctl(KERN_PROC_PID)`, or None (gone)."""
    buf = ctypes.create_string_buffer(KINFO_PROC_SIZE)
    size = ctypes.c_size_t(KINFO_PROC_SIZE)
    mib = (ctypes.c_int * 4)(CTL_KERN, KERN_PROC, KERN_PROC_PID, int(pid))
    if _libsystem().sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 16:
        return None
    sec, usec = buf.raw[0:8], buf.raw[8:12]
    return int.from_bytes(sec, "little", signed=True), int.from_bytes(usec, "little", signed=True)


# The hooks the module calls; a test replaces these names.
_proc_pidinfo = _native_proc_pidinfo
_getpeereid = _native_getpeereid
_host_cpu_ticks = _native_host_cpu_ticks
_vm_pages = _native_vm_pages
_sysctl_u64 = _native_sysctl_u64
_procargs = _native_procargs
_kinfo_start = _native_kinfo_start
_run = subprocess.run


# ── who is asking ───────────────────────────────────────────────────────────────────────────────────

def peer_of(conn) -> Peer:
    """The connecting process as the kernel reports it: `getpeereid` for the uid (stamped at `connect()`)
    and `LOCAL_PEERPID` for the pid — neither a field the peer sent. Raises `OSError` when the kernel
    cannot be asked.

    `LOCAL_PEERPID` is the peer socket's LAST user rather than its connector, so the pid is accepted only
    when that process's real uid is the connect-stamped one; otherwise the peer is unknown (fail closed).
    The audit token's pid version would pin it exactly — deferred until it is verified on hardware."""
    pid = int(conn.getsockopt(SOL_LOCAL, LOCAL_PEERPID))
    uid, _gid = _getpeereid(conn.fileno())
    if owner_uid(pid) != uid:
        log.warning("peer pid %s is not owned by the connecting uid %s — treating the peer as unknown",
                    pid, uid)
        return Peer(-1, str(uid))
    return Peer(pid, str(uid))


def _bsdinfo(pid: int) -> ProcBsdInfo | None:
    info = ProcBsdInfo()
    try:
        written = _proc_pidinfo(int(pid), PROC_PIDTBSDINFO, info)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log.debug("proc_pidinfo(%s) failed: %s", pid, exc)
        return None
    # A short write is a process that vanished mid-call, a flavour this kernel does not know — or another
    # uid's process, which the full flavour refuses to anyone but root.
    if written < ctypes.sizeof(info):
        return _short_bsdinfo(pid)
    return info


def _short_bsdinfo(pid: int) -> ProcBsdInfo | None:
    """`pid`'s identity when the full flavour is refused: the short flavour plus `kinfo_proc`'s start time.

    The kernel denies `PROC_PIDTBSDINFO` across uids (EPERM) unless the caller is root, and the daemon
    (`_jaahd`) asks almost only about the runner user's (`_jaah`) processes: every peer read as unknown and
    no clone got a git credential (measured on macOS 15). `PROC_PIDT_SHORTBSDINFO` is exempt from
    that same-user check and `sysctl(KERN_PROC_PID)` is what `ps` reads unprivileged; together they carry
    every field this module reads, and match the full flavour exactly."""
    short = ProcBsdShortInfo()
    try:
        written = _proc_pidinfo(int(pid), PROC_PIDT_SHORTBSDINFO, short)
        start = _kinfo_start(int(pid))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log.debug("short proc_pidinfo(%s) failed: %s", pid, exc)
        return None
    if written < ctypes.sizeof(short) or start is None:
        return None
    info = ProcBsdInfo()
    for name in ("pid", "ppid", "pgid", "status", "comm", "flags",
                 "uid", "gid", "ruid", "rgid", "svuid", "svgid"):
        setattr(info, f"pbi_{name}", getattr(short, f"pbsi_{name}"))
    info.pbi_start_tvsec, info.pbi_start_tvusec = start
    return info


def start_time(pid: int) -> int | None:
    """The process's start time in microseconds since the epoch (`pbi_start_tvsec/usec`), or None.

    Compared only for equality against a value this same call recorded, so its unit need not match
    Linux's boot ticks: a recycled pid has a different start time either way."""
    info = _bsdinfo(pid)
    if info is None:
        return None
    return int(info.pbi_start_tvsec) * 1_000_000 + int(info.pbi_start_tvusec)


# No mount namespaces here; the console names its tmux client by pid, so no stdin tty; only the Linux fleet
# server's recycle asks for children.
mount_namespace = stdin_tty = child_pids = absent


def parent_of(pid: int) -> int | None:
    """`pbi_ppid`, or None when the process cannot be read."""
    info = _bsdinfo(pid)
    return None if info is None else int(info.pbi_ppid)


def cmdline_args(pid: int) -> list | None:
    """`pid`'s argv elements (bytes) from `KERN_PROCARGS2`, or None when the process cannot be read.

    The area is `int argc`, the exec path, NUL padding, then `argc` NUL-terminated argv strings (the
    environment follows and is never read)."""
    raw = _procargs(int(pid))
    if raw is None or len(raw) < 4:
        # Gone is "no entry"; alive but unreadable (another uid's process) must never read as gone.
        return None if _bsdinfo(pid) is None else [b"<unreadable>"]
    argc = int.from_bytes(raw[:4], "little")
    rest = raw[4:]
    _exec_path, _sep, rest = rest.partition(b"\0")
    rest = rest.lstrip(b"\0")
    args = rest.split(b"\0")[:argc]
    return [arg for arg in args if arg]


def owner_uid(pid: int) -> int | None:
    """`pbi_uid` — the effective uid, as Linux's `/proc/<pid>` owner is — or None when unreadable."""
    info = _bsdinfo(pid)
    return None if info is None else int(info.pbi_uid)


def fd_path(fd: int) -> str:
    """`fcntl(fd, F_GETPATH)` — what the kernel says this descriptor opened. Raises `OSError`.
    Imported here: the Windows bundle imports this module, and `fcntl` does not exist there."""
    import fcntl

    buf = fcntl.fcntl(fd, F_GETPATH, b"\0" * MAXPATHLEN)
    return buf.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")


# ── host metrics ────────────────────────────────────────────────────────────────────────────────────

def read_cpu_totals() -> tuple | None:
    """`(total_ticks, busy_ticks)` from mach's cumulative CPU load counters, or None.

    `busy = total - idle`: Darwin has no iowait state to subtract."""
    ticks = _host_cpu_ticks()
    if not ticks or len(ticks) < 4:
        return None
    total = sum(ticks[:4])
    return (total, total - ticks[CPU_STATE_IDLE])


_FIXED: dict = {}


def _fixed(name: str) -> int | None:
    """A value that never changes while the machine runs (`hw.memsize`, `hw.pagesize`), read once."""
    if _FIXED.get(name) is None:
        _FIXED[name] = _sysctl_u64(name)
    return _FIXED[name]


def read_mem() -> tuple | None:
    """`(mem_pct, mem_total_kb)` from `hw.memsize` and mach's VM page counts, or None. No subprocess:
    this runs on the daemon's loop every tick.

    Available = free + inactive + speculative + purgeable pages — what the kernel can hand out without
    swapping, the Darwin counterpart of Linux's MemAvailable."""
    total_bytes, page_size = _fixed("hw.memsize"), _fixed("hw.pagesize")
    pages = _vm_pages()
    if not total_bytes or not page_size or pages is None:
        return None
    available = sum(pages.get(k, 0) for k in
                    ("free_count", "inactive_count", "speculative_count", "purgeable_count")) * page_size
    used_pct = 100.0 * (total_bytes - available) / total_bytes
    return (max(0.0, min(100.0, used_pct)), total_bytes // 1024)


def mem_available_mib() -> int | None:
    """`read_mem`'s available memory (free + inactive + speculative + purgeable pages) in MiB, or None:
    the Darwin counterpart of Linux's MemAvailable, for the MicroVM supervisor's start-time room check."""
    page_size, pages = _fixed("hw.pagesize"), _vm_pages()
    if not page_size or pages is None:
        return None
    return sum(pages.get(k, 0) for k in
               ("free_count", "inactive_count", "speculative_count", "purgeable_count")) * page_size // (1024 * 1024)


def sample_metrics() -> dict:
    """One instantaneous sample. macOS raises no "reboot required" flag and has no unattended-upgrades
    stamp, so both are None — the wire carries them as None, which the CP reads as "not reported"."""
    return metrics_sample(read_cpu_totals(), read_mem(), None, None)


# ── supervision ─────────────────────────────────────────────────────────────────────────────────────

def service(name: str, *, run=None) -> Service:
    """A launchd system daemon labelled `name`: `launchctl kickstart -k system/<name>` to restart,
    `launchctl print system/<name>` reporting `state = running` for active."""
    target = f"system/{name}"
    return Service(
        name,
        restart_argv=["launchctl", "kickstart", "-k", target],
        active_argv=["launchctl", "print", target],
        active_check=lambda proc: proc.returncode == 0 and "state = running" in (proc.stdout or ""),
        run=run or (lambda *a, **kw: _run(*a, **kw)),
    )


# ── the node's own IPv4 addresses and networks: getifaddrs ───────────────────────────────────────────

class _IfAddrs(ctypes.Structure):
    """`struct ifaddrs` — the same pointer layout in Darwin's libSystem and glibc."""


_IfAddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_IfAddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_dstaddr", ctypes.c_void_p),
    ("ifa_data", ctypes.c_void_p),
]

_AF_INET = 2
_IFF_POINTOPOINT = 0x10


def _sockaddr_in(ptr: int | None, *, bsd: bool) -> str | None:
    """The dotted IPv4 address of a `sockaddr_in`, or None when `ptr` is NULL or not AF_INET. BSD leads
    with `sa_len` then a one-byte family; glibc with a two-byte family. Both put the address at offset 4."""
    if not ptr:
        return None
    raw = ctypes.string_at(ptr, 8)
    family = raw[1] if bsd else int.from_bytes(raw[0:2], sys.byteorder)
    if family != _AF_INET:
        return None
    return ipaddress.IPv4Address(raw[4:8]).compressed


def _netmask(ptr: int | None, *, bsd: bool) -> str | None:
    """An AF_INET address's netmask. BSD hands it as a routing sockaddr: often `sa_family == 0` and an
    `sa_len` shorter than 8, the missing bytes zero — read only `sa_len` bytes, whatever the family."""
    if not ptr:
        return None
    if not bsd:
        return _sockaddr_in(ptr, bsd=False)
    size = ctypes.string_at(ptr, 1)[0]
    raw = ctypes.string_at(ptr, min(size, 8)) if size > 4 else b""
    return ipaddress.IPv4Address(raw[4:8].ljust(4, b"\0")).compressed


def getifaddrs_ipv4(lib, *, bsd: bool = True) -> tuple[set[str], list[ipaddress.IPv4Network]]:
    """`(addresses, networks)` of every IPv4 interface address `lib.getifaddrs` lists: each address, the
    network it sits on (address & netmask; never a default route), and a point-to-point link's peer as a
    /32. Raises `OSError` when the call fails: an empty answer would guard nothing."""
    lib.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(_IfAddrs))]
    lib.getifaddrs.restype = ctypes.c_int
    lib.freeifaddrs.argtypes = [ctypes.POINTER(_IfAddrs)]
    lib.freeifaddrs.restype = None
    head = ctypes.POINTER(_IfAddrs)()
    if lib.getifaddrs(ctypes.byref(head)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"getifaddrs: {os.strerror(err)}")
    addrs: set[str] = set()
    nets: list[ipaddress.IPv4Network] = []

    def add(net: ipaddress.IPv4Network) -> None:
        if net.prefixlen and net not in nets:
            nets.append(net)

    try:
        node = head
        while node:
            entry = node.contents
            address = _sockaddr_in(entry.ifa_addr, bsd=bsd)
            if address is not None:
                addrs.add(address)
                mask = _netmask(entry.ifa_netmask, bsd=bsd)
                if mask is not None:
                    add(ipaddress.IPv4Network(f"{address}/{mask}", strict=False))
                if entry.ifa_flags & _IFF_POINTOPOINT:
                    peer = _sockaddr_in(entry.ifa_dstaddr, bsd=bsd)
                    if peer is not None:
                        add(ipaddress.IPv4Network(f"{peer}/32"))
            node = entry.ifa_next
    finally:
        lib.freeifaddrs(head)
    return addrs, nets


def own_networks() -> tuple[set[str], list[ipaddress.IPv4Network]]:
    """The node's own IPv4 addresses and the networks its interfaces sit on, from libSystem's getifaddrs."""
    return getifaddrs_ipv4(ctypes.CDLL(_LIBSYSTEM, use_errno=True))
