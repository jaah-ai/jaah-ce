"""The daemon's host-platform seam: the ONE place jaahd learns which OS it runs on and how that OS answers
the handful of questions the daemon asks the kernel or the filesystem layout.

Every caller goes through the names below; nothing outside this package reads `/proc`, calls
`SO_PEERCRED` or `fcntl`, or spells a `/etc/jaah` / `/run/jaah` literal (pinned by
`tests/test_host_platform_guard.py`, which names the Linux-only subsystems that keep their own).

`current()` picks the implementation off `sys.platform` once, at import:

  | `sys.platform` | module       | state                                                         |
  |----------------|--------------|---------------------------------------------------------------|
  | `linux`        | `linux.py`   | today's Linux code, MOVED here — behaviour identical           |
  | `darwin`       | `macos.py`   | Apple Silicon: `LOCAL_PEERPID` + `getpeereid`, `libproc`, launchd |
  | `win32`        | `windows.py` | x64: a DACL'd named pipe, the client's token SID, Toolhelp32, SCM |

The interface (every module answers all of it):

  OS / ARCH / OS_VERSION       what the node reports in its observation (`platform_info()`)
  virtualization() / kvm()     the hypervisor it runs under and whether it can host KVM (None = unknown)
  hypervisor() / microvm_prereq()  the MicroVM hypervisor (`kvm`/`kvm-wsl2`/`vz`) and the first unmet
                               prerequisite for it, or None (MicroVM Architecture §9.1)
  paths() -> Paths             the filesystem layout; Linux values == the packaged literals (a community-edition
                               node's bundle and interpreter excepted: `linux._ce_paths`)
  serve_socket / connect_socket  the local IPC endpoint (AF_UNIX on posix)
  peer_of(conn) -> Peer        the connecting process as the KERNEL reports it, never a request field
  parent_of(pid) / start_time(pid)  the ancestry walk's two reads
  owner_uid(pid)               the real uid a process runs as
  cmdline_args(pid)            a process's argv, as the kernel holds it
  mount_namespace(pid) / child_pids(pid)  a process's mount namespace and direct children (None = unreadable)
  lock(fh) / unlock(fh)        an advisory whole-file lock
  sample_metrics() -> dict     one instantaneous host sample (`host_metrics` windows it)
  service(name)                restart / is-active for a supervised daemon unit
  fd_path(fd)                  what the kernel says an open descriptor names
  pane_os_pid(pane_pid, *, msys_start)  the OS pid behind a tmux `#{pane_pid}` (identity on posix; MSYS → Windows)
  path_key / is_under          compare paths in one spelling (MSYS `/c/x` == `C:\\x` on Windows)
  own_networks()               the node's own IPv4 addresses and networks (the MicroVM egress guard's)

Stdlib only and side-effect-free at import: `environment` (itself a stdlib-only leaf) imports this.
"""
from __future__ import annotations

import functools
import sys
import time
from typing import NamedTuple, Optional

from host_platform import _types
from host_platform._types import Paths, Peer, Service  # noqa: F401 — re-exported: the seam's own types

OS_LINUX = "linux"
OS_MACOS = "macos"
OS_WINDOWS = "windows"


def _select(platform: str):
    """The implementation module for a `sys.platform` value. Anything not darwin/win32 is Linux — the
    one platform the daemon has always run on, so an unrecognised posix never loses today's behaviour."""
    if platform == "darwin":
        from host_platform import macos as impl
    elif platform == "win32":
        from host_platform import windows as impl
    else:
        from host_platform import linux as impl
    return impl


_IMPL = _select(sys.platform)


def current():
    """The implementation module this process runs on."""
    return _IMPL


OS: str = _IMPL.OS
ARCH: str = _types.normalise_arch(_IMPL.machine())
OS_VERSION: str = _IMPL.os_version()


class PlatformInfo(NamedTuple):
    os: str
    arch: str
    os_version: str
    virtualization: Optional[str]
    kvm: Optional[bool]
    hypervisor: Optional[str]
    microvm_prereq: Optional[str]
    distro: Optional[dict]
    features: Optional[dict]


@functools.cache
def _machine_facts() -> tuple:
    """`(virtualization, kvm)`, asked once on first use rather than at import (a subprocess would break
    the side-effect-free import) and cached: neither changes while the daemon runs. Windows re-asks `kvm`
    itself once per pass (`windows.KVM_PROBE_TTL_S`), so it is read there through `_hypervisor_facts`."""
    return _IMPL.virtualization(), _IMPL.kvm()


def _hypervisor_facts() -> tuple:
    """`(kvm, hypervisor, microvm_prereq)`. `kvm` True carries no prerequisite (§9.1 precedence). An
    implementation that does not answer the last two reports them unknown."""
    if is_windows():
        kvm, prereq = _IMPL.microvm_facts()
    else:
        kvm = _machine_facts()[1]
        prereq = getattr(_IMPL, "microvm_prereq", lambda: None)()
    hypervisor = getattr(_IMPL, "hypervisor", lambda: None)()
    return kvm, hypervisor, None if kvm is True else prereq


#: How long an all-true host-feature probe answer stands: a package removed by a reprovision is seen without a
#: restart. An answer with any feature false is not reused: a feature appearing (the rootless-Docker marker a
#: converge writes, a mount helper installed) clears the node's `unsupported` reason on the next observation.
HOST_FEATURES_TTL_S = 600.0
_features_cache: list = []  # [(monotonic stamp, answer)] — at most one entry


def _host_facts() -> tuple:
    """`(distro, features)` — a Linux node's release and probed host features (`linux.distro`/`host_features`);
    None where this OS reports neither. The probes run binaries, so their answer is reused for
    `HOST_FEATURES_TTL_S` while every feature is present; a missing one is probed again each pass."""
    probe = getattr(_IMPL, "host_features", None)
    if probe is None:
        return None, None
    now = time.monotonic()
    if (not _features_cache or now - _features_cache[0][0] >= HOST_FEATURES_TTL_S
            or not all(_features_cache[0][1].values())):
        _features_cache[:] = [(now, probe())]
    return _IMPL.distro(), _features_cache[0][1]


def platform_info() -> dict:
    """The observation's `platform` block: `{os, arch, os_version, virtualization, kvm, hypervisor,
    microvm_prereq, distro, features}`, arch normalised to `x64`/`arm64`; the facts None where this OS cannot say.
    On Windows the `kvm` probe can take seconds, and on Linux the feature probes run binaries: call this off the
    event loop."""
    return PlatformInfo(OS, ARCH, OS_VERSION, _machine_facts()[0], *_hypervisor_facts(), *_host_facts())._asdict()


def is_linux() -> bool:
    return OS == OS_LINUX


def is_windows() -> bool:
    return OS == OS_WINDOWS


def is_macos() -> bool:
    return OS == OS_MACOS


# The seam's functions, bound once. Callers use `host_platform.<name>`, never the module behind it, so a
# test fakes the seam by replacing the name here.
paths = _IMPL.paths
serve_socket = _IMPL.serve_socket
connect_socket = _IMPL.connect_socket
peer_of = _IMPL.peer_of
parent_of = _IMPL.parent_of
start_time = _IMPL.start_time
owner_uid = _IMPL.owner_uid
cmdline_args = _IMPL.cmdline_args
mount_namespace = _IMPL.mount_namespace
child_pids = _IMPL.child_pids
stdin_tty = _IMPL.stdin_tty
lock = _IMPL.lock
unlock = _IMPL.unlock
sample_metrics = _IMPL.sample_metrics
service = _IMPL.service
fd_path = _IMPL.fd_path
pane_os_pid = _IMPL.pane_os_pid
path_key = _IMPL.path_key
is_under = _IMPL.is_under
own_networks = _IMPL.own_networks
