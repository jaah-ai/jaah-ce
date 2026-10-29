"""Windows x64: the seam answered by the Windows kernel — a DACL'd named pipe for the local endpoint,
`GetNamedPipeClientProcessId` + the client's process-token SID for who is asking, Toolhelp32 +
`GetProcessTimes` for the ancestry walk, `msvcrt.locking` for locks, `GetSystemTimes` /
`GlobalMemoryStatusEx` for metrics, and the Service Control Manager for supervision.

Layout (Architecture §4.2): the bundle under `C:\\Program Files\\Jaah` (the active release at `current`),
config in `C:\\ProgramData\\Jaah\\etc\\node.env`, the session user's jaah home at `C:\\ProgramData\\Jaah\\home`,
the credential broker on the pipe `\\\\.\\pipe\\jaah-creds`, and the node-managed engine (the `jaah-engine`
WSL2 distro) on `npipe:////./pipe/jaah_docker`.

Who may reach the broker pipe (PRD R7): its DACL is PROTECTED and names exactly three principals — SYSTEM
and the daemon's own account with full control, and the runner account `jaah` with read/write DATA only.
`jaah` is deliberately not granted `FILE_CREATE_PIPE_INSTANCE` (which `GENERIC_WRITE` would carry), so a
runner can never stand up a server instance of the daemon's pipe; the first instance is created with
`FILE_FLAG_FIRST_PIPE_INSTANCE`, so the daemon never serves a name someone else squatted first.

Who is asking: only the kernel. `peer_of` reads the client pid off the pipe handle and the SID
the kernel holds for the connection (by impersonating the client); nothing the client sends is consulted.
The daemon runs as its own service account, which can open neither a runner's token nor a native runner
process, so nothing here reads another account's token: creation times and parents come from the
kernel's process table (`_process_table`), and "this process is the runner account's" is ancestry to the
fleet tmux server, verified against the Service Control Manager (`fleet_server_anchor`). Windows recycles
pids freely, so every pid this module vouches for is pinned by creation time: a peer created AFTER its
connection was accepted is a reused pid (unknown peer), and a parent created AFTER its child is a reused
pid (no parent).

A client trusts the pipe only once its OWNER is verified to be SYSTEM or the daemon's service
(`NT SERVICE\\jaahd`), so a runner that creates the name while the daemon is down serves nobody.

Every native call goes through one module-level hook (`_create_pipe`, `_client_pid`, `_pipe_owner`,
`_pin_process`, `_impersonated_sid`, `_process_table`, `_service_process`, `_fleet_server_msys_pid`, `_kill_fleet_server`, `_fleet_server_busy`,
`_attested_server`, `_image_name`,
`_msys_winpid`, `_msys_table`,
`_creation_time`, `_toolhelp_ppid`, `_lookup_account`, `_current_sid`,
`_system_times`, `_memory_status`,
`_open_pipe`, `_pipe_read`, `_pipe_write`, `_close_handle`, `_msvcrt`, `_accept_pipe`, `_make_transport`,
`_read_msys_proc`, `_msys_proc_absent`, `_pane_owner_check`, `_file_owner`, `_path_owner`, `_final_path`,
`_create_new_fd`, `_delete_by_handle`, `_private_dir`, `_private_file`, `_private_open`, `_handle_facts`,
`_file_security`, `_set_dacl`, `_git_install_path`, `_run`) so
`tests/test_host_platform_windows.py` drives this module on Linux with fakes. Nothing native is loaded at import: this module imports on Linux in tests.

The terminal route (W1): MSYS2's tmux, run on Git for Windows' OWN `msys-2.0.dll`, so the tmux
server, its clients and every pane shell share one MSYS runtime — one process table (`/proc`), one mount
table (Git's `/usr/bin` is `/bin`, its `/bin/sh` runs `run-shell`), and real ptys between them. The bundle
ships `bin\\tmux.exe` and the DLLs it imports from MSYS2, but NOT `msys-2.0.dll`: the loader finds Git's on
PATH, which is why every process that runs tmux has `git_path_dirs()` on PATH (`git_path_dirs`). The
fleet socket is named after the runner account, not a uid (`session_user.fleet_socket`): MSYS uids are
not Windows identities. A pane is identified by its MSYS pid and MSYS start time, which survive the
pane's `exec`; `pane_os_pid` maps that to the CURRENT Windows pid through `/proc/<pid>/winpid`, read
with Git's own `cat.exe`.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import logging
import ntpath
import os
import platform as _platform
import re
import stat
import subprocess
import sys
import time

from host_platform._types import absent, UNKNOWN_PEER, Paths, Peer, Service, metrics_sample

OS = "windows"

_PATHS = Paths(
    etc_dir="C:\\ProgramData\\Jaah\\etc",
    run_dir="C:\\ProgramData\\Jaah\\run",
    creds_socket="\\\\.\\pipe\\jaah-creds",
    state_dir="C:\\ProgramData\\Jaah\\state",
    bundle_root="C:\\Program Files\\Jaah",
    lib_root="C:\\Program Files\\Jaah\\current",
    # The bundle's interpreter (the bundle build stages it under runtime/python).
    python="C:\\Program Files\\Jaah\\current\\runtime\\python\\python.exe",
    home_root="C:\\ProgramData\\Jaah\\home",
    docker_host="npipe:////./pipe/jaah_docker",
    # TMUX_TMPDIR: the fleet socket lives one level down, in a directory named after the runner account.
    tmux="C:\\ProgramData\\Jaah\\run\\tmux",
    # `JaahService.cs`'s `Layout.Log`: each service's `<role>.log`.
    log_dir="C:\\ProgramData\\Jaah\\log",
)

# The dedicated account every runner process runs as (the binding "dedicated service user" decision).
# A deployment invariant the installer owns: `jaah` is in no privileged group (Administrators, Backup
# Operators) and holds no SeRestorePrivilege — with it, a squatted pipe could claim `O:SY`.
RUNNER_ACCOUNT = "jaah"
# The daemon's Windows service. Its service SID (`NT SERVICE\jaahd`) is what the pipe DACL names and what a
# client requires of the server, so the installer must register it with `sc sidtype jaahd unrestricted`.
DAEMON_SERVICE = "jaahd"

log = logging.getLogger("jaahd.host_platform.windows")


def machine() -> str:
    return _platform.machine()


def os_version() -> str:
    return _platform.version()


def paths() -> Paths:
    return _PATHS


# Unknown: `systemd-detect-virt` is Linux's, and nothing here answers it as cheaply.
virtualization = absent


# ── the MicroVM hypervisor: KVM inside WSL2 (MicroVM Architecture §2.3, §9.1) ─────────────────────────
#
# A Windows MicroVM node runs Cloud Hypervisor on the KVM that WSL2's utility VM exposes. That utility VM and
# its `/dev/kvm` are shared by every distro, so the node-managed engine distro answers for it before the
# MicroVM distro exists. A WSL distro belongs to the account that registered it, so the probe runs AS THE
# RUNNER ACCOUNT (`session_user.run_as_session_user`), as `nodelink_windows` does.

HYPERVISOR = "kvm-wsl2"
#: The node-managed engine (the Windows service host, `JaahService.cs`).
ENGINE_DISTRO = "jaah-engine"
#: For the runner account's shell (Git's MSYS `sh`): `/bin/sh` and every other argument handed to the NATIVE
#: wsl.exe must reach it unconverted (MSYS rewrites a leading `/` into a Windows path); wsl.exe's own
#: messages in UTF-8, as `JaahService.cs` asks.
WSL_ENV = {"MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*", "WSL_UTF8": "1"}
#: The first build whose WSL2 exposes nested KVM: Windows 11. No server build does: WSL documents
#: `nestedVirtualization` as Windows 11 only, and Windows Server 2025 on EC2 metal (m7i.metal-24xl, hardware
#: VT-x) ran WSL2 with no `/dev/kvm` (probe P3).
CLIENT_MIN_BUILD = 22000
KVM_PROBE_TIMEOUT_S = 10.0
#: One answer per observation pass: `kvm()` and `microvm_prereq()` share it, and a pass a minute apart re-asks
#: (enabling nested virtualisation needs no daemon restart).
KVM_PROBE_TTL_S = 60.0
#: Usable, not merely present: the device opens read-write (`microvmd.kvm_probe`'s test, as the distro's root).
_KVM_PROBE_SH = "test -c /dev/kvm && exec 3<>/dev/kvm"
# `(monotonic time, (kvm, microvm_prereq))` of the last answer, or None.
_KVM_FACTS: list = [None]


def wsl_exe() -> str:
    return ntpath.join(os.environ.get("SystemRoot") or "C:\\Windows", "System32", "wsl.exe")


def _windows_build() -> tuple[int, bool] | None:
    """`(build, is_server)` of this Windows, or None off Windows. `product_type` 1 is a workstation; a
    domain controller (2) and a server (3) are both server builds."""
    get = getattr(sys, "getwindowsversion", None)
    if get is None:
        return None
    version = get()
    return int(version.build), int(version.product_type) != 1


def _wsl(argv: list[str]) -> int | None:
    """Exit code of `wsl.exe ARGV` run as the runner account, or None when it could not be run at all
    (no runner tmux server yet, or the deadline passed)."""
    import session_user  # noqa: PLC0415 — imports this package; the probe is the daemon's, never at import

    try:
        proc = session_user.run_as_session_user([wsl_exe(), *argv], env=dict(WSL_ENV), timeout=KVM_PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log.info("microvm prereq: `wsl %s` did not answer within %.0fs", " ".join(argv[:2]), KVM_PROBE_TIMEOUT_S)
        return None
    except (RuntimeError, OSError) as exc:
        log.info("microvm prereq: could not run wsl.exe as the runner account (%s)", exc)
        return None
    return proc.returncode


def _wsl_answers() -> bool | None:
    """WSL 2 is installed: `--version` answers on the Store WSL even before any distro exists, `--status` on
    the inbox one (the bootstrap's `Test-Wsl`). None = could not ask."""
    if not os.path.isfile(wsl_exe()):
        return False
    codes = [_wsl(["--version"])]
    if codes[0] != 0:
        codes.append(_wsl(["--status"]))
    if 0 in codes:
        return True
    return None if None in codes else False


def _engine_kvm() -> bool | None:
    """Whether `/dev/kvm` opens read-write inside WSL2; None = could not ask."""
    code = _wsl(["-d", ENGINE_DISTRO, "-u", "root", "--exec", "sh", "-c", _KVM_PROBE_SH])
    return None if code is None else code == 0


def _probe_kvm_facts() -> tuple[bool | None, str | None]:
    """`(kvm, microvm_prereq)`: the first unmet prerequisite in the §9.1 order — `windows_build`, `wsl_missing`,
    `wsl_no_kvm` — or `(True, None)`. `(None, None)` when the answer could not be had this pass."""
    build = _windows_build()
    if build is not None and (build[1] or build[0] < CLIENT_MIN_BUILD):
        return False, "windows_build"
    usable = _engine_kvm()
    if usable:
        return True, None
    # The probe failed: no WSL at all, or WSL without KVM.
    installed = _wsl_answers()
    if installed is False:
        return False, "wsl_missing"
    if usable is None or installed is None:
        return None, None
    return False, "wsl_no_kvm"


def _kvm_facts() -> tuple[bool | None, str | None]:
    """`_probe_kvm_facts`, asked at most once per `KVM_PROBE_TTL_S`."""
    cached = _KVM_FACTS[0]
    if cached is not None and time.monotonic() - cached[0] < KVM_PROBE_TTL_S:
        return cached[1]
    facts = _probe_kvm_facts()
    # Stamped once answered: the probe itself can take three `KVM_PROBE_TIMEOUT_S`.
    _KVM_FACTS[0] = (time.monotonic(), facts)
    return facts


def kvm() -> bool | None:
    """Whether WSL2 exposes a usable `/dev/kvm` (`_kvm_facts`); None = could not ask this pass."""
    return _kvm_facts()[0]


def hypervisor() -> str:
    return HYPERVISOR


def microvm_prereq() -> str | None:
    """The first unmet MicroVM prerequisite (§9.1), or None — always None while `kvm()` is True."""
    return _kvm_facts()[1]


def microvm_facts() -> tuple[bool | None, str | None]:
    """`(kvm(), microvm_prereq())` from ONE answer, so the pair never straddles a re-probe."""
    return _kvm_facts()


def pane_os_pid(pane_pid: int, *, msys_start: int | None = None) -> int | None:
    """The CURRENT Windows pid of the MSYS process a tmux pane reports as `pane_pid`, or None.

    A pane's identity is (MSYS pid, MSYS start time), not a Windows pid: the pane runs bash, which `exec`s
    the runner, and under MSYS an exec keeps the MSYS pid while the Windows process behind it changes. So
    the Windows pid is re-read every time. The start time (field 22 of `/proc/<pid>/stat`, recorded when
    the pane was first seen) is RE-STAMPED by each `exec` (measured on a real PC: a pane
    recorded before its `exec claude` never re-mapped, and every runner was refused), so it moves only
    forward: a pane re-maps while its start time is no earlier than the one recorded. An MSYS pid is not
    reused while its process lives, and naming a reused one needs the runtime's pid counter to wrap after
    the pane ended and before the registry noticed (`pane_gone`). None (`msys_start` omitted) checks no
    start time — for a caller that has just been handed the pid by tmux. Rules: `pane_identity`."""
    ident = pane_identity(pane_pid)
    if ident is None:
        return None
    if msys_start is not None and ident[1] < msys_start:
        return None
    return ident[0]


def pane_identity(pane_pid: int) -> tuple[int, int] | None:
    """(current Windows pid, MSYS start time) of the MSYS process `pane_pid`, or None.

    Read from `/proc/<pid>/winpid` and `/proc/<pid>/stat` with Git's `cat.exe` (`_read_msys_proc`), and
    vouched for only when every rule holds, so a reused pid on either side never resolves:
      * the MSYS process is not a zombie (state `Z`): a zombie's `winpid` names an exited process, whose
        number may already be someone else's;
      * the Windows pid is a positive number, and the start time a number;
      * both reads are taken around a creation-time read of the Windows pid and must agree, and that
        creation time must read the same twice: neither the mapping nor the process it names changed
        while we looked (a Windows pid reused mid-read never resolves);
      * the Windows process runs as the runner account (`_pane_owner_check`).
    Every failure is None, which every ancestry walk treats as "no such process"."""
    try:
        msys_pid = int(pane_pid)
    except (TypeError, ValueError):
        return None
    if msys_pid <= 0:
        return None
    cached = _PANE_CACHE.get(msys_pid)
    if cached is not None:
        expires, winpid, msys_start, created = cached
        # A hit is re-validated, cheaply: the Windows process it names is still the one whose creation time
        # was vouched for, AND the runtime still maps the pane to it. An `exec` into a native program leaves
        # the old Windows process alive as a stub, so the creation time alone kept the pre-exec pid: the
        # runner's own claim then walked past it and was refused (a real PC). Any miss re-reads.
        if _monotonic() < expires and start_time(winpid) == created and _still_mapped(msys_pid, winpid):
            return winpid, msys_start
        _PANE_CACHE.pop(msys_pid, None)
    first = _msys_identity(msys_pid)
    if first is None:
        return None
    created = start_time(first[0])
    if created is None:
        return None
    if _msys_identity(msys_pid) != first or start_time(first[0]) != created:
        return None
    # The MSYS process table is the runtime's own data, writable by any process on it: whatever it names,
    # only a process running AS the runner account is ever vouched for as a pane.
    if not _pane_owner_check(first[0]):
        return None
    _PANE_CACHE[msys_pid] = (_monotonic() + _PANE_CACHE_TTL_S, first[0], first[1], created)
    return first


def _still_mapped(msys_pid: int, winpid: int) -> bool:
    """Whether the MSYS process table still maps `msys_pid` to `winpid`; False when it cannot be read."""
    try:
        entry = _msys_table_for(winpid).get(int(msys_pid))
    except OSError:
        return False
    return entry is not None and entry[1] == int(winpid)


# Vouched-for pane identities, so a broker call does not spawn two `cat.exe` per runner on every request.
# Short, and every hit re-checked against the kernel (`pane_identity`).
_PANE_CACHE: dict[int, tuple[float, int, int, int]] = {}
_PANE_CACHE_TTL_S = 5.0
_monotonic = time.monotonic


def pane_gone(pane_pid: int, msys_start: int) -> bool:
    """Whether the pane recorded as (`pane_pid`, `msys_start`) has DEFINITELY ended: its `/proc` entry is
    absent, it is a zombie, or the pid now belongs to a process that started before the pane was recorded
    (the start time only moves forward, on `exec` — `pane_os_pid`).
    A read that merely failed (a spawn error, a timeout) is NOT an ending: a registry prunes only on
    this, so one transient failure never deletes a live runner's identity."""
    try:
        msys_pid = int(pane_pid)
    except (TypeError, ValueError):
        return True
    if _msys_proc_absent(msys_pid):
        return True
    text = _read_msys_proc(msys_pid)
    if text is None:
        return False
    lines = text.splitlines()
    _comm, sep, rest = (lines[1] if len(lines) > 1 else "").rpartition(")")
    fields = rest.split() if sep else []
    if len(fields) < 20 or not fields[19].isdigit():
        return False
    return fields[0] == "Z" or int(fields[19]) < int(msys_start)


# ── whose process: ancestry to the runner service, never a token read ───────────────────────────────
#
# The daemon's account (`NT SERVICE\jaahd`, no administrator) cannot open a runner process's token: Git's
# MSYS runtime gives every token it creates a DACL of the account, Administrators and SYSTEM only, and a
# native runner process refuses even `OpenProcess` (measured on a real PC). So "this process is
# the runner account's" is proven by ANCESTRY instead, off the kernel's process table, which needs no right:
#
#   * the ANCHOR is the fleet tmux server. `fleet_tmux_server` runs it in the foreground (`tmux -D`) as a
#     child of the `jaah-runner` service's loop, so it descends from the process the Service Control
#     Manager reports for `jaah-runner`, whose logon account the SCM reports as `.\jaah`. Verified ONCE,
#     while that chain is alive, then held by (pid, creation time) in the daemon's own state dir: the
#     server outlives a restart of the service (its panes must), and a restarted daemon finds it there.
#     A server whose chain was cut before it was ever pinned is verified instead by the runner account's own
#     record of starting it (`attest_fleet_server`, `_attests`); one with neither proof is replaced
#     (`_replace_stale_server`);
#   * a process is the runner account's when it descends from the anchor (`_descends`). A hop is the
#     Windows parent while that parent is alive and no younger than its child; where it is not, and the
#     process is an MSYS one, the hop is its MSYS parent (`_msys_parent`). MSYS `exec` of another MSYS
#     program replaces the Windows process and lets the old one exit, so a pane's bash, the runner it
#     execs and every `timeout` in a delegated script have a dead Windows parent (measured on a real PC);
#     the MSYS parent table keeps POSIX parentage across that. A process names another as its
#     Windows parent only by holding PROCESS_CREATE_PROCESS on it, and writes only its own MSYS process
#     entry, so either route is forgeable by the runner account alone — the same "every runner shares the
#     `jaah` account" limit `parent_of` states.

FLEET_RUNNER_SERVICE = "jaah-runner"
_ANCHOR_FILE = "fleet-server-anchor.json"
# Server <- loop <- service: a hop of slack for an interpreter shim.
_ANCHOR_MAX_HOPS = 4
_OWNER_MAX_DEPTH = 64
_ANCHOR: list = [None]
# Why the last verification failed, or None: `(stale, text)`. `stale` is the (pid, creation time) of a live server
# under no live jaah-runner loop — one left from before the service restarted — which the daemon replaces
# (`pin_fleet_server_anchor`); None when there is nothing to replace.
_ANCHOR_PROBLEM: list = [None]
# Consecutive stale pins (60 s apart) before the stale server is replaced: one pass of slack for a restart in flight.
_REPLACE_AFTER = 2
_STALE_RUN: list = [0]
# The stale server last replaced, until an anchor verifies again: one kill per verified-anchor gap, so a chain
# `_descends` cannot walk on some PC never becomes a kill every two minutes (its successor stays, a Health finding).
_REPLACED: list = [None]


def _msys_table_for(pid: int) -> dict[int, tuple[int, int]]:
    """`_msys_table()`, re-read once when it does not hold `pid`: the snapshot is reused for a second, and a
    process started within it (a delegated shell being admitted) must not read as untracked. Raises
    `OSError`."""
    msys = _msys_table()
    if not any(winpid == int(pid) for _ppid, winpid in msys.values()):
        _MSYS_TABLE_CACHE[1] = None
        msys = _msys_table()
    return msys


def _msys_hop(table: dict, msys: dict, pid: int) -> int | None:
    """The current Windows pid of `pid`'s live MSYS parent, when `pid` is a process the MSYS runtime tracks
    (its own programs, and the native ones it started); else None.

    No age test: the runtime does not always re-parent an orphan (its parent pid can name an exited
    process), so the parent must be in the same snapshot; MSYS pids come from a counter, so naming a reused
    one needs the counter to wrap while the child lives."""
    own = next(((mpid, mppid) for mpid, (mppid, winpid) in msys.items() if winpid == int(pid)), None)
    if own is None or own[1] <= 1:
        return None
    up = msys.get(own[1])
    return up[1] if up is not None and up[1] in table and up[1] != pid else None


def _hybrid_parent(table: dict, msys: dict, pid: int) -> int | None:
    """`pid`'s parent in one snapshot: its MSYS parent when the runtime tracks it (`_msys_hop`), else its
    Windows parent while alive and no younger than it. MSYS first: a tracked process's Windows parent is
    often a stub the runtime left on `exec`, whose own parent has exited (a real PC)."""
    entry = table.get(int(pid))
    if entry is None:
        return None
    hop = _msys_hop(table, msys, pid)
    if hop is not None:
        return hop
    ppid = entry[0]
    parent = table.get(ppid)
    if ppid not in (0, pid) and parent is not None and parent[1] <= entry[1]:
        return ppid
    return None


def _descends(table: dict, pid: int, ancestor: tuple[int, int], max_depth: int, msys: dict | None = None) -> bool:
    """Whether `pid` is, or descends from, `ancestor` (pid, creation time) in `table` (`_hybrid_parent`'s
    hops; Windows hops only when `msys` is None)."""
    current = int(pid)
    for _depth in range(max_depth + 1):
        entry = table.get(current)
        if entry is None:
            return False
        if (current, entry[1]) == ancestor:
            return True
        parent = _hybrid_parent(table, msys or {}, current)
        if parent is None:
            return False
        current = parent
    return False


def _is_runner_account(account: str) -> bool:
    """Whether an SCM logon name (`.\\jaah`, `COMPUTER\\jaah`) is the local runner account."""
    domain, sep, name = (account or "").rpartition("\\")
    return bool(sep) and name.lower() == RUNNER_ACCOUNT and domain.upper() in (".", _computer_name().upper())


def _runner_service(table: dict) -> tuple[int, int] | None:
    """(pid, creation time) of the `jaah-runner` service process, when the SCM runs it as the runner account."""
    reported = _service_process(FLEET_RUNNER_SERVICE)
    if reported is None or not _is_runner_account(reported[1]):
        return None
    entry = table.get(reported[0])
    return (reported[0], entry[1]) if entry else None


def _anchor_path() -> str:
    return ntpath.join(paths().state_dir, _ANCHOR_FILE)


def _load_anchor() -> tuple[int, int] | None:
    try:
        with open(_anchor_path(), encoding="ascii") as fh:
            data = json.load(fh)
        return int(data["pid"]), int(data["created"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _store_anchor(anchor: tuple[int, int]) -> None:
    path = _anchor_path()
    try:
        with open(path + ".tmp", "w", encoding="ascii") as fh:
            json.dump({"pid": anchor[0], "created": anchor[1]}, fh)
        os.replace(path + ".tmp", path)
    except OSError as exc:
        log.warning("fleet server anchor: could not persist %s (%s) — a daemon restart re-verifies it, "
                    "which fails once the jaah-runner service has restarted", path, exc)


def fleet_server_anchor(table: dict, *, warn: bool = True) -> tuple[int, int] | None:
    """(pid, creation time) of the verified fleet tmux server, or None (fail closed). See the block above.
    `warn=False` logs a failure at debug (the periodic pin, which warns once per failure run)."""
    for anchor in (_ANCHOR[0], _load_anchor()):
        if anchor is not None and table.get(anchor[0], (None, None))[1] == anchor[1]:
            _ANCHOR[0] = anchor
            _ANCHOR_PROBLEM[0] = None
            _REPLACED[0] = None
            return anchor
    _ANCHOR[0] = None
    msys_pid = _fleet_server_msys_pid()
    server = _msys_winpid(msys_pid) if msys_pid else None
    service = _runner_service(table)
    say = log.warning if warn else log.debug
    if server is None or service is None:
        _ANCHOR_PROBLEM[0] = (None, f"cannot verify the fleet tmux server (server {server}, jaah-runner "
                                     f"service {service[0] if service else None})")
        say("fleet server anchor: %s — no runner process can be identified until it is", _ANCHOR_PROBLEM[0][1])
        return None
    entry = table.get(server)
    proof = f"under jaah-runner pid {service[0]}"
    if entry is not None and not _descends(table, server, service, _ANCHOR_MAX_HOPS) \
            and _attests(table, server, _attested_server(table)):
        # The chain is cut — `jaah-runner` restarted (every update does) and the server outlived its loop
        # (`KillMode=process`) — but the loop that started it, as the runner account, recorded it
        # (`attest_fleet_server`): its runners stay identifiable, with nothing to replace (win3).
        proof = "by the runner account's record of starting it"
    elif entry is None or not _descends(table, server, service, _ANCHOR_MAX_HOPS):
        _ANCHOR_PROBLEM[0] = ((server, entry[1]) if entry else None, f"the fleet tmux server (pid {server}) does not run under the "
                                                 f"jaah-runner service (pid {service[0]})")
        say("fleet server anchor: %s — a server left from before the service last restarted, or an older "
            "daemonised one; the daemon replaces it. Until then no runner process can be identified",
            _ANCHOR_PROBLEM[0][1])
        return None
    anchor = (server, entry[1])
    _ANCHOR[0] = anchor
    _ANCHOR_PROBLEM[0] = None
    _REPLACED[0] = None
    _store_anchor(anchor)
    log.info("fleet server anchor: verified tmux server pid %s %s", server, proof)
    return anchor


def fleet_server_attestation_path(socket: str) -> str:
    """The runner account's record of the fleet server it started, beside that server's socket."""
    return ntpath.join(ntpath.dirname(socket), _ATTESTATION_FILE)


def attest_fleet_server(socket: str, pid: int) -> None:
    """Record, as the runner account, that `pid` is the fleet server this loop started on `socket`: its
    (pid, creation time), so a daemon that never pinned it can still anchor it after `jaah-runner` restarts
    (`_attested_server`). Raises `OSError`."""
    created = start_time(pid)
    if created is None:
        raise OSError(errno.ESRCH, f"the fleet tmux server (pid {pid}) cannot be dated")
    path = fleet_server_attestation_path(socket)
    with open(path + ".tmp", "w", encoding="ascii") as fh:
        json.dump({"pid": int(pid), "created": created}, fh)
    os.replace(path + ".tmp", path)


def _attests(table: dict, server: int, attested: tuple[int, int] | None) -> bool:
    """Whether the runner account's record vouches for `server`, the process that answered on the fleet socket:
    `server` IS the recorded start (or its child, should the start be a launcher stub), is a tmux image, and that
    start is no ancestor of this daemon. A runner can write the record and forge the socket's answer, so neither
    picks the anchor alone: anchoring `wininit` would make every process on the machine the runner account's,
    and anchoring by the socket file's owner trusts whoever binds a freed loopback port (security review)."""
    if attested is None or not _descends(table, server, attested, _ATTESTED_MAX_HOPS):
        return False
    try:
        if (_image_name(server) or "").casefold() != _TMUX_IMAGE:
            return False
    except Exception:  # noqa: BLE001 — a snapshot that failed in any way is no proof
        return False
    return not _descends(table, os.getpid(), attested, _OWNER_MAX_DEPTH)


def _native_attested_server(table: dict) -> tuple[int, int] | None:
    """(pid, creation time) of the fleet server the runner account recorded starting, when that record is the
    runner account's and still names a live process in `table`; else None (fail closed: any failure to read
    or parse it, which only a runner can cause, is no proof)."""
    path = fleet_server_attestation_path(_fleet_socket_path())
    try:
        with open(path, encoding="ascii") as fh:
            # The owner of the file this handle opened, not of whatever the path names a moment later.
            if file_owner_sid(fh.fileno()) != runner_sid():
                log.warning("fleet server anchor: %s is not the runner account's — ignored", path)
                return None
            data = json.load(fh)
        attested = int(data["pid"]), int(data["created"])
    except Exception as exc:  # noqa: BLE001 — fail closed on anything: OSError, a malformed or hostile record
        log.debug("fleet server attestation unreadable: %s", exc)
        return None
    live = table.get(attested[0])
    return attested if live is not None and live[1] == attested[1] else None


_ATTESTATION_FILE = "server-attest.json"
# The answering server is the recorded start, or one hop below it.
_ATTESTED_MAX_HOPS = 1
_TMUX_IMAGE = "tmux.exe"


def pin_fleet_server_anchor() -> bool:
    """Verify and persist the fleet server anchor now, rather than at the first runner's call: the daemon's
    periodic step. Verifying needs the server -> loop -> service chain alive, and the first restart of
    `jaah-runner` (every update does one) breaks it while the server lives on. A node whose first runner
    came after that restart refused every runner as `not_a_runner` until a reboot (win1)."""
    try:
        table = _process_table()
    except OSError as exc:
        log.warning("fleet server anchor: process table unreadable (%s)", exc)
        _ANCHOR_PROBLEM[0] = (None, f"the process table could not be read ({exc})")
        return False
    # Warned once when it starts failing, not every minute while it stays so.
    pinned = fleet_server_anchor(table, warn=_PINNED[0] is not False) is not None
    _PINNED[0] = pinned
    # One snapshot: a runner's call verifies on another thread and may rewrite the problem meanwhile.
    problem = _ANCHOR_PROBLEM[0]
    target = problem[0] if not pinned and problem else None
    _STALE_RUN[0] = _STALE_RUN[0] + 1 if target is not None and _REPLACED[0] is None else 0
    if target is not None and _STALE_RUN[0] >= _REPLACE_AFTER:
        _replace_stale_server(target, problem[1], table)
    return pinned


def _replace_stale_server(target: tuple[int, int], why: str, table: dict) -> None:
    """Re-anchor by replacement: a server outliving a jaah-runner restart can never be verified again, and every
    runner on it is already refused `not_a_runner`. The loop's next tick starts its own child, and the next pin
    verifies that one; the runners die with the old one. Never while a person is attached or a delegated job runs
    (retried next pass), never a server other than `target`, and once until an anchor verifies again."""
    if _fleet_server_busy():
        _STALE_RUN[0] = _REPLACE_AFTER - 1
        log.info("fleet server anchor: the stale fleet tmux server is in use; replacing it once it is idle")
        return
    _STALE_RUN[0] = 0
    msys_pid = _fleet_server_msys_pid()
    server = _msys_winpid(msys_pid) if msys_pid else None
    if server is None or (server, table.get(server, (None, None))[1]) != target:
        return  # already replaced by the loop's own recycle: the next pin judges the new one
    if _ANCHOR[0] is not None:
        return  # a runner's call verified a server since the pin read it: never kill that one
    _REPLACED[0] = target
    killed = _kill_fleet_server()
    if killed:
        _ANCHOR_PROBLEM[0] = None
    log.warning("fleet server anchor: %s the stale fleet tmux server (%s)",
                "replaced" if killed else "could not replace", why)


def fleet_anchor_problem() -> str | None:
    """Why the fleet server anchor is unverified, as the last pin found it; None when it verified."""
    problem = _ANCHOR_PROBLEM[0]
    return problem[1] if problem else None


# The last pin's outcome (None before the first).
_PINNED: list = [None]


def _fleet_socket_path() -> str:
    """The fleet tmux server's socket (`session_user.fleet_socket` on this node)."""
    return ntpath.join(paths().tmux, RUNNER_ACCOUNT, "default")


def _fleet_tmux(*args: str) -> subprocess.CompletedProcess:
    """One tmux command on the fleet server's socket, as the daemon."""
    socket = _fleet_socket_path()
    tmux = ntpath.join(paths().lib_root, "bin", "tmux.exe")
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*git_path_dirs(), env.get("PATH", "")])
    env["MSYS"] = with_msys_noglob(env.get("MSYS"))
    return _run([tmux, "-S", socket, *args], capture_output=True, text=True, timeout=10, env=env)


def _native_fleet_server_msys_pid() -> int | None:
    """The fleet tmux server's own MSYS pid (`#{pid}`), asked of the server on its socket, or None."""
    try:
        proc = _fleet_tmux("display-message", "-p", "#{pid}")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("fleet server pid: %s", exc)
        return None
    text = (proc.stdout or "").strip()
    return int(text) if proc.returncode == 0 and text.isdigit() else None


def _native_fleet_server_busy() -> bool:
    """A client is attached to the fleet server, or a delegated `run-shell` job may still run on it (the loop's own
    recycle asks the same: `fleet_tmux_server._delegated_job_in_flight`). Unreadable counts as busy."""
    import fleet_tmux_server  # noqa: PLC0415 — the loop's module; its spool read is the one home of that answer
    try:
        clients = _fleet_tmux("list-clients")
    except (OSError, subprocess.SubprocessError):
        return True
    return clients.returncode != 0 or bool((clients.stdout or "").strip()) or fleet_tmux_server._delegated_job_in_flight()


def _native_kill_fleet_server() -> bool:
    """`kill-server` on the fleet socket; whether tmux accepted it."""
    try:
        return _fleet_tmux("kill-server").returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("fleet server anchor: kill-server failed: %s", exc)
        return False


def _native_msys_winpid(msys_pid: int) -> int | None:
    """`/proc/<msys_pid>/winpid` through Git's `cat.exe`, or None. Only that file: a process the runner
    started natively (the `-D` server, under the service's loop) carries the native DACL, so its `stat`
    is "Permission denied" to the daemon's account while `winpid` is the runtime's own (a real PC, Task
    4744). Its creation time comes from the process table instead."""
    cat = ntpath.join(git_root(), "usr", "bin", "cat.exe")
    try:
        proc = _run([cat, f"/proc/{int(msys_pid)}/winpid"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("reading /proc/%s/winpid failed: %s", msys_pid, exc)
        return None
    text = (proc.stdout or "").strip()
    return int(text) if proc.returncode == 0 and text.isdigit() and int(text) > 0 else None


_MSYS_TABLE_TTL_S = 1.0
_MSYS_TABLE_CACHE: list = [0.0, None]


def _native_msys_table() -> dict[int, tuple[int, int]]:
    """`{msys pid: (msys parent pid, winpid)}` of every process on Git's MSYS runtime, from ONE `ps -a`
    (Git's own, so it sees the runtime the tmux server and its panes run on), reused for a second so a
    walk spawns it once rather than per hop. Raises `OSError`."""
    now = _monotonic()
    if _MSYS_TABLE_CACHE[1] is not None and now - _MSYS_TABLE_CACHE[0] < _MSYS_TABLE_TTL_S:
        return dict(_MSYS_TABLE_CACHE[1])
    table = _read_msys_table()
    _MSYS_TABLE_CACHE[:] = [now, table]
    return dict(table)


def _read_msys_table() -> dict[int, tuple[int, int]]:
    ps = ntpath.join(git_root(), "usr", "bin", "ps.exe")
    try:
        proc = _run([ps, "-a"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(errno.EIO, f"{ps} -a failed: {exc}") from exc
    if proc.returncode != 0:
        raise OSError(errno.EIO, f"{ps} -a exited {proc.returncode}")
    table: dict[int, tuple[int, int]] = {}
    for line in (proc.stdout or "").splitlines()[1:]:
        fields = line.split()
        # A leading one-letter status column (`I`, `S`, `O`) precedes the pid on some rows.
        if fields and not fields[0].isdigit():
            fields = fields[1:]
        if len(fields) >= 4 and all(f.isdigit() for f in fields[:4]):
            table[int(fields[0])] = (int(fields[1]), int(fields[3]))
    return table


def _native_runner_owned(pid: int) -> bool:
    """Whether process `pid` is the runner account's: it descends from the verified fleet tmux server."""
    try:
        table = _process_table()
    except OSError as exc:
        log.warning("process table unreadable (%s) — no process is vouched for as a runner's", exc)
        return False
    anchor = fleet_server_anchor(table)
    if anchor is None:
        return False
    try:
        msys = _msys_table_for(int(pid))
    except OSError as exc:
        log.debug("MSYS process table unreadable (%s) — Windows parents only", exc)
        msys = {}
    return _descends(table, int(pid), anchor, _OWNER_MAX_DEPTH, msys)


def _msys_identity(msys_pid: int) -> tuple[int, int] | None:
    """(`/proc/<msys_pid>/winpid`, field-22 starttime of its `stat`), or None when unreadable, malformed,
    or the process is a zombie."""
    text = _read_msys_proc(msys_pid)
    if text is None:
        return None
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    winpid, stat = lines[0].strip(), lines[1]
    # `/proc/<pid>/stat` is `pid (comm) state ...`; comm may hold spaces or parentheses, so fields are
    # counted from the LAST `)`: the state is field 3, the start time field 22.
    _comm, sep, rest = stat.rpartition(")")
    fields = rest.split() if sep else []
    if len(fields) < 20 or fields[0] == "Z" or not winpid.isdigit() or not fields[19].isdigit():
        return None
    value = int(winpid)
    return (value, int(fields[19])) if value > 0 else None


def _native_read_msys_proc(msys_pid: int) -> str | None:
    """`cat /proc/<pid>/winpid /proc/<pid>/stat` through Git's MSYS runtime — the runtime the tmux server
    and its panes run on, so it sees the same process table. None on any failure."""
    cat = ntpath.join(git_root(), "usr", "bin", "cat.exe")
    try:
        proc = _run([cat, f"/proc/{int(msys_pid)}/winpid", f"/proc/{int(msys_pid)}/stat"],
                    capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("reading /proc/%s through %s failed: %s", msys_pid, cat, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or None


def _native_msys_proc_absent(msys_pid: int) -> bool:
    """True only when Git's `cat.exe` RAN and reported `/proc/<pid>` missing — the one definitive "this
    MSYS process is gone". Any other outcome (a spawn error, a timeout, success) is False."""
    cat = ntpath.join(git_root(), "usr", "bin", "cat.exe")
    try:
        proc = _run([cat, f"/proc/{int(msys_pid)}/stat"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode != 0 and "No such file" in (proc.stderr or "")


# A worktree sweep, run AS THE SESSION USER by `worktree_mechanics.reap._reap_windows_tree`: every MSYS process
# whose cwd is in the tree gets TERM, then KILL after the grace.
# The runtime's own `/proc` gives a cwd for each process it tracks (its programs and the native ones it
# started); `$$` is excluded, and the delegated shell's own cwd is not the tree. Paths compare
# case-insensitively, as NTFS resolves them: a process that `cd`'d in by a lower-cased spelling is in the tree.
REAP_TREE_SH = r"""
pids=$(for d in /proc/[0-9]*; do
  c=$(readlink "$d/cwd" 2>/dev/null) || continue
  printf '%s\t%s\n' "${d#/proc/}" "$c"
done | awk -F '\t' -v t="$SU_TREE" -v self="$$" '
  BEGIN { t = tolower(t) }
  $1 != self { c = tolower($2); if (c == t || index(c, t "/") == 1) printf " %s", $1 }')
[ -n "$pids" ] || exit 0
echo "$pids"
kill -TERM $pids 2>/dev/null
sleep "$SU_GRACE"
for p in $pids; do [ -d "/proc/$p" ] && kill -KILL "$p" 2>/dev/null; done
exit 0
"""


# ── Git for Windows: the shell runners, hooks and the terminal run under ───────────────────────────────

# The installer's preflight requires Git for Windows installed for all users (apps/api/app/node_bootstrap.py).
GIT_ROOT_ENV = "JAAH_GIT_ROOT"
_GIT_ROOT_DEFAULT = "C:\\Program Files\\Git"


def _native_git_install_path() -> str:
    """Git for Windows' `InstallPath` from HKLM (what its installer writes), or "" when absent."""
    try:
        import winreg  # noqa: PLC0415 — Windows-only stdlib, imported where it is used
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\GitForWindows") as key:
            value, _kind = winreg.QueryValueEx(key, "InstallPath")
        return str(value or "")
    except (ImportError, OSError):
        return ""


def git_root() -> str:
    """Git for Windows' install directory: `$JAAH_GIT_ROOT` when set (jaahd.env), else the registry's
    `InstallPath`, else `C:\\Program Files\\Git`. Never a per-user install: the installer refuses one."""
    return (os.environ.get(GIT_ROOT_ENV, "").strip() or _git_install_path().strip() or _GIT_ROOT_DEFAULT)


def git_bash() -> str:
    """Git Bash's `bash.exe` — the value of `CLAUDE_CODE_GIT_BASH_PATH` and the shell every pane runs."""
    return ntpath.join(git_root(), "bin", "bash.exe")


def with_msys_noglob(value: str | None) -> str:
    """The `MSYS` environment value with `noglob` set — what every MSYS program a NATIVE process starts
    (tmux, Git's sh) must see. Without it the runtime globs and brace-expands each unquoted argument of its
    Windows command line: tmux's `-F '#{pane_pid}'` arrives as `#pane_pid`, and a `*` in a pane command as
    a file list (measured on a real PC). Other `MSYS` options are kept."""
    options = [opt for opt in (value or "").split() if opt not in ("glob", "noglob")]
    return " ".join([*options, "noglob"])


def git_path_dirs() -> list[str]:
    """The Git directories a process that runs tmux, or runs inside a pane, needs on PATH, in order:
    `usr\\bin` first (its `msys-2.0.dll` is the one `tmux.exe` binds to, and its coreutils are what
    `run-shell` scripts call), then `cmd` (git.exe) and `mingw64\\bin`."""
    root = git_root()
    return [ntpath.join(root, "usr", "bin"), ntpath.join(root, "cmd"), ntpath.join(root, "mingw64", "bin")]


#: Where the installer puts Node.js (copies held equal by a test): the interpreter every npm-installed runner bin (`claude-agent-acp`'s `.cmd`
#: shim) and every Node hook a runner's CLI runs (oh-my-claudecode's) needs on a pane's PATH.
NODEJS_DIR = "C:\\Program Files\\nodejs"


# MSYS's own spelling of a path, which tmux reports (`#{pane_current_path}`): `/c/x/y` for `C:\\x\\y`.
_MSYS_DRIVE_RE = re.compile(r"^/(?:cygdrive/)?([A-Za-z])(?=/|$)")


def path_key(path: str) -> str:
    """A path in one comparable spelling: MSYS `/c/x` and Windows `C:\\x`/`C:/x` agree, case-folded (NTFS
    is case-insensitive) and without a trailing separator."""
    text = (path or "").strip()
    match = _MSYS_DRIVE_RE.match(text)
    if match:
        text = f"{match.group(1)}:{text[match.end():]}"
    text = text.replace("/", "\\")
    if len(text) > 3:
        text = text.rstrip("\\")
    return text.casefold()


def is_under(path: str, root: str) -> bool:
    """Whether `path` is `root` or inside it, whichever spelling (MSYS or Windows) each uses."""
    p, r = path_key(path), path_key(root)
    if not p or not r:
        return False
    return p == r or p.startswith(r.rstrip("\\") + "\\")


# ── Win32 constants ─────────────────────────────────────────────────────────────────────────────────

# CreateNamedPipeW
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
_PIPE_BUFFER = 65536

# File / pipe access rights (winnt.h).
FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_CREATE_PIPE_INSTANCE = 0x0004  # == FILE_APPEND_DATA; GENERIC_WRITE carries it
FILE_READ_EA = 0x0008
FILE_READ_ATTRIBUTES = 0x0080
FILE_WRITE_ATTRIBUTES = 0x0100
READ_CONTROL = 0x00020000
SYNCHRONIZE = 0x00100000

# What the runner account is granted on the broker pipe: be a client, and nothing more. Never widen it to
# FILE_CREATE_PIPE_INSTANCE: every instance shares the daemon's descriptor, so an instance a runner added
# would pass the client's owner check (`_verify_server`) and receive other runners' requests.
RUNNER_PIPE_RIGHTS = (FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_EA | FILE_READ_ATTRIBUTES
                      | FILE_WRITE_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE)
# What a client opens the pipe with — inside RUNNER_PIPE_RIGHTS, so `GENERIC_READ|GENERIC_WRITE` (whose
# FILE_GENERIC_WRITE asks for FILE_APPEND_DATA) is never requested and never refused.
CLIENT_PIPE_ACCESS = FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE

# CreateFileW for the client: identification-level impersonation only, so a server cannot act AS the client.
OPEN_EXISTING = 3
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000

ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_BROKEN_PIPE = 109
ERROR_SEM_TIMEOUT = 121
ERROR_PIPE_BUSY = 231
ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
TOKEN_GROUPS_CLASS = 2
# SID_NAME_USE values an account lookup must come back as.
SID_TYPE_USER = 1
SID_TYPE_WELL_KNOWN_GROUP = 5
SYSTEM_SID = "S-1-5-18"
# Service SIDs (`NT SERVICE\<name>`) are S-1-5-80-<hash of the name>.
_SERVICE_SID_PREFIX = "S-1-5-80-"
_PIPE_PREFIX = "\\\\.\\pipe\\"
TH32CS_SNAPPROCESS = 0x00000002
SE_KERNEL_OBJECT = 6
OWNER_SECURITY_INFORMATION = 0x00000001

# FILETIME (100 ns ticks since 1601-01-01 UTC) of the Unix epoch.
_EPOCH_AS_FILETIME = 116_444_736_000_000_000

# A SID's string form, as `ConvertSidToStringSidW` writes it — nothing else may reach an SDDL string.
_SID_RE = re.compile(r"S-1-[0-9]+(-[0-9]+)+")
# A service name the SCM accepts and a command line cannot be steered by.
_SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,256}")
# `sc.exe query`'s state line — never a `RUNNING` that appears in the service's own name.
_SC_RUNNING = re.compile(r"^\s*STATE\s*:\s*\d+\s+RUNNING\b", re.MULTILINE)


def filetime_now() -> int:
    """Now, as a FILETIME — the clock `GetProcessTimes` creation times are on."""
    return time.time_ns() // 100 + _EPOCH_AS_FILETIME


# ── native bindings, loaded on first use ────────────────────────────────────────────────────────────

_NATIVE: dict = {}


def _win():
    """kernel32 + advapi32 with every function this module calls typed — once per process."""
    if "k32" not in _NATIVE:
        from ctypes import wintypes as w

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        H, D, B, P = w.HANDLE, w.DWORD, w.BOOL, ctypes.c_void_p
        PD, PU64 = ctypes.POINTER(w.DWORD), ctypes.POINTER(ctypes.c_uint64)
        for fn, args, res in (
            (k32.CreateNamedPipeW, [w.LPCWSTR, D, D, D, D, D, D, P], H),
            (k32.GetNamedPipeClientProcessId, [H, ctypes.POINTER(w.ULONG)], B),
            (k32.OpenProcess, [D, B, D], H),
            (k32.GetProcessTimes, [H, PU64, PU64, PU64, PU64], B),
            (k32.CloseHandle, [H], B),
            (k32.LocalFree, [P], P),
            (k32.CreateToolhelp32Snapshot, [D, D], H),
            (k32.Process32FirstW, [H, P], B),
            (k32.Process32NextW, [H, P], B),
            (k32.GetSystemTimes, [PU64, PU64, PU64], B),
            (k32.GlobalMemoryStatusEx, [P], B),
            (k32.GetCurrentProcess, [], H),
            (k32.GetFinalPathNameByHandleW, [H, w.LPWSTR, D, D], D),
            (k32.CreateFileW, [w.LPCWSTR, D, D, P, D, D, H], H),
            (k32.SetFileInformationByHandle, [H, ctypes.c_int, P, D], B),
            (adv.OpenProcessToken, [H, D, ctypes.POINTER(H)], B),
            (adv.GetTokenInformation, [H, ctypes.c_int, P, D, PD], B),
            (adv.ConvertSidToStringSidW, [P, ctypes.POINTER(w.LPWSTR)], B),
            (adv.ConvertStringSecurityDescriptorToSecurityDescriptorW,
             [w.LPCWSTR, D, ctypes.POINTER(P), ctypes.POINTER(w.ULONG)], B),
            (adv.GetSecurityInfo, [H, ctypes.c_int, D, ctypes.POINTER(P), P, P, P, ctypes.POINTER(P)], D),
            (adv.SetSecurityInfo, [H, ctypes.c_int, D, P, P, P, P], D),
            (adv.GetSecurityDescriptorControl, [P, ctypes.POINTER(w.WORD), PD], B),
            (adv.GetSecurityDescriptorDacl, [P, ctypes.POINTER(B), ctypes.POINTER(P), ctypes.POINTER(B)], B),
            (adv.GetAce, [P, D, ctypes.POINTER(P)], B),
            (k32.CreateDirectoryW, [w.LPCWSTR, P], B),
            (k32.GetFileInformationByHandle, [H, P], B),
            (adv.GetNamedSecurityInfoW,
             [w.LPCWSTR, ctypes.c_int, D, ctypes.POINTER(P), P, P, P, ctypes.POINTER(P)], D),
            (adv.LookupAccountNameW, [w.LPCWSTR, w.LPCWSTR, P, PD, w.LPWSTR, PD, PD], B),
            (adv.ImpersonateNamedPipeClient, [H], B),
            (adv.RevertToSelf, [], B),
            (adv.OpenThreadToken, [H, D, B, ctypes.POINTER(H)], B),
            (k32.GetCurrentThread, [], H),
            (adv.OpenSCManagerW, [w.LPCWSTR, w.LPCWSTR, D], H),
            (adv.OpenServiceW, [H, w.LPCWSTR, D], H),
            (adv.QueryServiceStatusEx, [H, ctypes.c_int, P, D, PD], B),
            (adv.QueryServiceConfigW, [H, P, D, PD], B),
            (adv.CloseServiceHandle, [H], B),
        ):
            fn.argtypes, fn.restype = args, res
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, P, w.ULONG, ctypes.POINTER(w.ULONG)]
        ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        _NATIVE["k32"], _NATIVE["adv"], _NATIVE["ntdll"] = k32, adv, ntdll
    return _NATIVE["k32"], _NATIVE["adv"]


def _last_error(what: str) -> OSError:
    err = ctypes.get_last_error()  # type: ignore[attr-defined]
    return OSError(errno.EACCES if err == ERROR_ACCESS_DENIED else errno.EIO, f"{what} failed",
                   None, err)


_INVALID_HANDLE = ctypes.c_void_p(-1).value


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", ctypes.c_uint32), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.c_int)]


class ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32), ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_uint16 * 260),  # WCHAR[MAX_PATH]: 2 bytes on Windows, unlike c_wchar on Linux
    ]


class MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _native_create_pipe(name: str, sddl: str, first: bool) -> int:
    """One overlapped, local-only, byte-mode server instance of pipe `name`, guarded by `sddl`."""
    k32, adv = _win()
    sd, size = ctypes.c_void_p(), ctypes.c_ulong()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd),
                                                                     ctypes.byref(size)):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), sd, 0)
        open_mode = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
        handle = k32.CreateNamedPipeW(
            name, open_mode, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES, _PIPE_BUFFER, _PIPE_BUFFER, 0, ctypes.byref(sa))
        failure = _last_error(f"CreateNamedPipeW({name})")   # captured before LocalFree can overwrite it
    finally:
        k32.LocalFree(sd)
    if handle in (None, _INVALID_HANDLE):
        raise failure
    return int(handle)


# What the runner account may do to a console relay's output file (`create_runner_writable_file`): write and
# append to it, read its attributes — FILE_GENERIC_WRITE | FILE_READ_ATTRIBUTES, which an MSYS `cat >>` opens
# with. No read, no delete, no WRITE_DAC: it can feed the file and nothing more.
RUNNER_OUTPUT_RIGHTS = 0x00120196
_GENERIC_WRITE = 0x40000000
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80


def runner_output_sddl(daemon_sid: str, runner_sid: str) -> str:
    """A console relay output file's descriptor: owned by the daemon, a PROTECTED DACL (nothing inherited)
    giving SYSTEM and the daemon full control and the runner account `RUNNER_OUTPUT_RIGHTS`."""
    for sid in (daemon_sid, runner_sid):
        if not isinstance(sid, str) or not _SID_RE.fullmatch(sid):
            raise ValueError(f"not a string SID: {sid!r}")
    return (f"O:{daemon_sid}D:P(A;;FA;;;SY)(A;;FA;;;{daemon_sid})"
            f"(A;;0x{RUNNER_OUTPUT_RIGHTS:08x};;;{runner_sid})")


def _native_create_file(path: str, sddl: str) -> None:
    """Create `path` (CREATE_NEW: never an existing file) under `sddl`. Raises `OSError`."""
    k32, adv = _win()
    sd, size = ctypes.c_void_p(), ctypes.c_ulong()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd),
                                                                     ctypes.byref(size)):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), sd, 0)
        handle = k32.CreateFileW(path, _GENERIC_WRITE, 0, ctypes.byref(sa), _CREATE_NEW, _FILE_ATTRIBUTE_NORMAL,
                                 None)
        failure = _last_error(f"CreateFileW({path})")
    finally:
        k32.LocalFree(sd)
    if handle in (None, _INVALID_HANDLE):
        raise failure
    k32.CloseHandle(handle)


def create_runner_writable_file(path: str) -> None:
    """A new, empty file the runner account may append to and the daemon owns (`runner_output_sddl`), for a
    runner-side process to feed the daemon through. Put it in a daemon-only directory, so the runner can
    neither replace it nor point it elsewhere. Raises `OSError`."""
    _create_file(path, runner_output_sddl(current_sid(), runner_sid()))


_GENERIC_READ = 0x80000000
_DELETE = 0x00010000
_FILE_SHARE_READ_WRITE = 0x00000003  # the CRT `_wopen` default: no FILE_SHARE_DELETE
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_ERROR_FILE_EXISTS = 80
_FILE_DISPOSITION_INFO = 4  # FILE_INFO_BY_HANDLE_CLASS.FileDispositionInfo


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]  # BOOLEAN


def _native_create_new_fd(path: str) -> int:
    """A read-write, binary CRT descriptor on a NEW file at `path`. Raises `FileExistsError` when the name
    is taken, else `OSError`."""
    k32, _ = _win()
    handle = k32.CreateFileW(path, _GENERIC_READ | _GENERIC_WRITE | _DELETE, _FILE_SHARE_READ_WRITE, None,
                             _CREATE_NEW, _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle in (None, _INVALID_HANDLE):
        if ctypes.get_last_error() == _ERROR_FILE_EXISTS:  # type: ignore[attr-defined]
            raise FileExistsError(errno.EEXIST, "the name is taken", path)
        raise _last_error(f"CreateFileW({path})")
    try:
        # No `_O_TEXT`, so the descriptor is binary.
        return _msvcrt().open_osfhandle(int(handle), 0)
    except BaseException:
        k32.CloseHandle(handle)
        raise


def _native_delete_by_handle(fd: int) -> None:
    """Mark the file behind CRT descriptor `fd` for deletion when its last handle closes. Raises `OSError`."""
    k32, _ = _win()
    info = _FileDispositionInfo(1)
    if not k32.SetFileInformationByHandle(_msvcrt().get_osfhandle(fd), _FILE_DISPOSITION_INFO,
                                          ctypes.byref(info), ctypes.sizeof(info)):
        raise _last_error("SetFileInformationByHandle(FileDispositionInfo)")


def create_new_fd(path: str) -> int:
    """`os.open(path, O_RDWR | O_CREAT | O_EXCL | O_BINARY)` as Windows can make it safe: never through a
    final link or junction (`FILE_FLAG_OPEN_REPARSE_POINT`: a planted one is a taken name), and holding
    DELETE access, so a create that landed somewhere it must not be is taken back BY HANDLE
    (`delete_by_handle`) — a parent junction is still followed, which only the handle's final path shows.
    Raises `FileExistsError` when the name is taken, else `OSError`."""
    return _create_new_fd(str(path))


def delete_by_handle(fd: int) -> None:
    """Delete the file open on `fd` (a `create_new_fd` descriptor: it needs DELETE access) by its handle, not
    its name — the name may already lead somewhere else. Gone once `fd` is closed. Raises `OSError`."""
    _delete_by_handle(int(fd))


# ── The NodeVault keystore's own access lists (`nodevault_private`) ─────────────────────────────────

# Who may hold an entry of the keystore, and who an access list may grant: this daemon's token user, SYSTEM,
# and the Administrators group. Nobody else — the runner account least of all.
ADMINISTRATORS_SID = "S-1-5-32-544"
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
# Every DENY ACE type: a deny narrows, so it never needs repairing.
DENY_ACE_TYPES = frozenset({1, 6, 10, 12})
_WRITE_DAC = 0x00040000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_DELETE = 0x4
_FILE_SHARE_ALL = 0x7
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ALREADY_EXISTS = 183
# The CRT flags `open_osfhandle` takes (fcntl.h); no `_O_TEXT`, so the descriptor is binary.
_O_RDONLY, _O_WRONLY, _O_BINARY = 0x0, 0x1, 0x8000


def keystore_sddl(owner_sid: str, *, directory: bool) -> str:
    """A keystore entry's descriptor: owned by `owner_sid` (the daemon's token user), a PROTECTED DACL giving
    SYSTEM, Administrators and the daemon full control — nothing inherited, nobody else. A directory's ACEs
    also inherit to what is created below it."""
    if not isinstance(owner_sid, str) or not _SID_RE.fullmatch(owner_sid):
        raise ValueError(f"not a string SID: {owner_sid!r}")
    flags = "OICI" if directory else ""
    return f"O:{owner_sid}D:P(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)(A;{flags};FA;;;{owner_sid})"


class _AclHeader(ctypes.Structure):
    _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte), ("AclSize", ctypes.c_ushort),
                ("AceCount", ctypes.c_ushort), ("Sbz2", ctypes.c_ushort)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_ushort)]


# ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE: header, mask, then the SID.
_ACE_SID_OFFSET = ctypes.sizeof(_AceHeader) + 4


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [("dwFileAttributes", ctypes.c_uint32), ("ftCreationTime", ctypes.c_uint32 * 2),
                ("ftLastAccessTime", ctypes.c_uint32 * 2), ("ftLastWriteTime", ctypes.c_uint32 * 2),
                ("dwVolumeSerialNumber", ctypes.c_uint32), ("nFileSizeHigh", ctypes.c_uint32),
                ("nFileSizeLow", ctypes.c_uint32), ("nNumberOfLinks", ctypes.c_uint32),
                ("nFileIndexHigh", ctypes.c_uint32), ("nFileIndexLow", ctypes.c_uint32)]


def _with_sd(sddl: str, use):
    """`use(sd)` with `sddl` converted to a self-relative descriptor, freed after. The Win32 error of a call
    inside `use` is captured by it, before `LocalFree` can overwrite it."""
    k32, adv = _win()
    sd, size = ctypes.c_void_p(), ctypes.c_ulong()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), ctypes.byref(size)):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        return use(sd)
    finally:
        k32.LocalFree(sd)


def _native_private_dir(path: str, sddl: str) -> bool:
    """`CreateDirectoryW(path)` under `sddl`. False when the name is taken (the caller checks what is there)."""
    k32, _ = _win()

    def create(sd):
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), sd, 0)
        if k32.CreateDirectoryW(path, ctypes.byref(sa)):
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:  # type: ignore[attr-defined]
            return False
        raise _last_error(f"CreateDirectoryW({path})")
    return _with_sd(sddl, create)


def _native_private_file(path: str, sddl: str) -> int:
    """A write-only, binary CRT descriptor on a NEW file at `path` under `sddl`, shared with nobody while open.
    Raises `FileExistsError` when the name is taken, else `OSError`."""
    k32, _ = _win()

    def create(sd):
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), sd, 0)
        handle = k32.CreateFileW(path, _GENERIC_WRITE, 0, ctypes.byref(sa), _CREATE_NEW, _FILE_ATTRIBUTE_NORMAL,
                                 None)
        if handle in (None, _INVALID_HANDLE):
            if ctypes.get_last_error() in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):  # type: ignore[attr-defined]
                raise FileExistsError(errno.EEXIST, "the name is taken", path)
            raise _last_error(f"CreateFileW({path})")
        return handle
    handle = _with_sd(sddl, create)
    try:
        return _msvcrt().open_osfhandle(int(handle), _O_WRONLY | _O_BINARY)
    except BaseException:
        k32.CloseHandle(handle)
        raise


def _native_private_open(path: str, directory: bool) -> int | None:
    """A binary CRT descriptor on the keystore entry at `path` itself, never what a link or junction there
    points at (`FILE_FLAG_OPEN_REPARSE_POINT`), holding `READ_CONTROL | WRITE_DAC` so its access list can be
    checked and repaired through the same handle. A file is opened for reading and shares DELETE, so a
    rename over it or a removal is never refused by a reader. None when absent; else raises `OSError`."""
    k32, _ = _win()
    if directory:
        access, share = READ_CONTROL | _WRITE_DAC | FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL
        flags = _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS
    else:
        access, share = _GENERIC_READ | READ_CONTROL | _WRITE_DAC, _FILE_SHARE_READ | _FILE_SHARE_DELETE
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
    handle = k32.CreateFileW(path, access, share, None, OPEN_EXISTING, flags, None)
    if handle in (None, _INVALID_HANDLE):
        if ctypes.get_last_error() in (ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):  # type: ignore[attr-defined]
            return None
        raise _last_error(f"CreateFileW({path})")
    try:
        return _msvcrt().open_osfhandle(int(handle), _O_RDONLY | _O_BINARY)
    except BaseException:
        k32.CloseHandle(handle)
        raise


def _native_handle_facts(fd: int) -> tuple[int, int]:
    """`(attributes, link count)` of the file behind CRT descriptor `fd` (`GetFileInformationByHandle`)."""
    k32, _ = _win()
    info = _ByHandleFileInformation()
    if not k32.GetFileInformationByHandle(_msvcrt().get_osfhandle(fd), ctypes.byref(info)):
        raise _last_error("GetFileInformationByHandle")
    return int(info.dwFileAttributes), int(info.nNumberOfLinks)


def _native_file_security(fd: int) -> tuple[str, bool, list[tuple[int, str | None]]]:
    """`(owner SID, DACL protected, [(ACE type, SID)])` of the file behind `fd`. A SID is None for an ACE type
    whose layout is not the plain allowed/denied one; a NULL DACL (everyone, everything) reads as one
    allowed ACE for Everyone. Raises `OSError`."""
    k32, adv = _win()
    owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    err = adv.GetSecurityInfo(_msvcrt().get_osfhandle(fd), SE_FILE_OBJECT,
                              OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION, ctypes.byref(owner), None,
                              ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise OSError(errno.EACCES if err == ERROR_ACCESS_DENIED else errno.EIO, "GetSecurityInfo failed", None, err)
    try:
        control, revision = ctypes.c_ushort(), ctypes.c_ulong()
        if not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            raise _last_error("GetSecurityDescriptorControl")
        if not dacl.value:
            return _sid_string(owner), bool(control.value & _SE_DACL_PROTECTED), [(ACCESS_ALLOWED_ACE_TYPE, "S-1-1-0")]
        aces: list[tuple[int, str | None]] = []
        for index in range(ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents.AceCount):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                raise _last_error("GetAce")
            kind = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents.AceType
            plain = kind in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE)
            aces.append((int(kind), _sid_string(ctypes.c_void_p((ace.value or 0) + _ACE_SID_OFFSET)) if plain else None))
        return _sid_string(owner), bool(control.value & _SE_DACL_PROTECTED), aces
    finally:
        k32.LocalFree(sd)


def _native_set_dacl(fd: int, sddl: str) -> None:
    """Replace the DACL of the file behind `fd` with `sddl`'s, PROTECTED (nothing inherited). Raises `OSError`."""
    _k32, adv = _win()

    def apply(sd):
        present, dacl, defaulted = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_int()
        if not adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise _last_error("GetSecurityDescriptorDacl")
        err = adv.SetSecurityInfo(_msvcrt().get_osfhandle(fd), SE_FILE_OBJECT,
                                  _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                                  None, None, dacl, None)
        if err:
            raise OSError(errno.EACCES if err == ERROR_ACCESS_DENIED else errno.EIO, "SetSecurityInfo failed",
                          None, err)
    _with_sd(sddl, apply)


def create_private_dir(path: str, sddl: str) -> bool:
    """Create the keystore directory `path` under `sddl`; False when the name is taken. Raises `OSError`."""
    return _private_dir(str(path), sddl)


def create_private_file(path: str, sddl: str) -> int:
    """`os.open(path, O_WRONLY | O_CREAT | O_EXCL | O_BINARY)` with `sddl` as the new file's descriptor.
    Raises `FileExistsError` when the name is taken, else `OSError`."""
    return _private_file(str(path), sddl)


def open_private(path: str, *, directory: bool = False) -> int | None:
    """A descriptor on the keystore entry at `path` (`_native_private_open`), or None when absent."""
    return _private_open(str(path), directory)


def handle_facts(fd: int) -> tuple[int, int]:
    """`(attributes, link count)` of the file open on `fd`."""
    return _handle_facts(int(fd))


def file_security(fd: int) -> tuple[str, bool, list[tuple[int, str | None]]]:
    """`(owner SID, DACL protected, [(ACE type, SID | None)])` of the file open on `fd`."""
    return _file_security(int(fd))


def set_protected_dacl(fd: int, sddl: str) -> None:
    """Replace the DACL of the file open on `fd` with `sddl`'s, protected."""
    _set_dacl(int(fd), sddl)


_LINK_HOPS = 40


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0)
                                               & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _anchored(path: str) -> tuple[str, list[str]]:
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    return drive + os.sep, [part for part in rest.split(os.sep) if part]


def _local_link_target(raw: str) -> str | None:
    """A link's target with the kernel's `\\\\?\\`/`\\??\\` prefix dropped, or None for anything but a local
    drive path or a relative name: UNC, a device or a volume GUID."""
    for prefix in ("\\\\?\\", "\\??\\"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            return raw if raw[1:3] == ":\\" else None
    return None if raw.startswith(("\\\\", "//")) else raw


def local_real_path(path: str) -> str | None:
    """`os.path.realpath(path)` resolved by READING each link and junction on it (`os.lstat`, `os.readlink`),
    never by opening through one, or None when one leads off the local disks (UNC, a device), cannot be
    read, or loops. `realpath` opens the whole path, so a runner's link to `\\\\host\\share` makes the daemon
    authenticate to that host (NTLM) before any check could refuse it. `..` is lexical, as Windows has it."""
    if path.startswith(("\\\\", "//")):
        return None
    resolved, parts = _anchored(path)
    hops = 0
    while parts:
        candidate = os.path.join(resolved, parts.pop(0))
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            return os.path.join(candidate, *parts)  # nothing below an absent name is a link
        except (OSError, ValueError):
            return None
        if not _is_link(info):
            resolved = candidate
            continue
        hops += 1
        try:
            target = _local_link_target(os.readlink(candidate))
        except (OSError, ValueError):
            return None
        if target is None or hops > _LINK_HOPS:
            return None
        resolved, head = _anchored(os.path.join(resolved, target))
        parts = head + parts
    return resolved


def local_path_under(path: str, root: str) -> bool:
    """Whether a daemon may OPEN `path` in a runner-writable tree: it is not itself a link or junction, and
    `local_real_path` keeps it on the local disks and under ROOT's real path. Checked BEFORE the open — no
    `O_NOFOLLOW` here, so the open would follow whatever the runner planted; the handle's final path is the
    check after. An absent `path` passes, for the open to report."""
    real, real_root = local_real_path(str(path)), local_real_path(str(root))
    if real is None or real_root is None:
        return False
    try:
        if _is_link(os.lstat(path)):
            return False
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return False
    return ntpath.normcase(real).startswith(ntpath.normcase(real_root).rstrip("\\") + "\\")


def _native_client_pid(handle: int) -> int:
    k32, _ = _win()
    pid = ctypes.c_ulong()
    if not k32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        raise _last_error("GetNamedPipeClientProcessId")
    return int(pid.value)


def _native_pipe_owner(handle: int) -> str:
    """The owner SID of the pipe behind `handle` (`GetSecurityInfo`, needs READ_CONTROL). Raises `OSError`."""
    k32, adv = _win()
    owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
    err = adv.GetSecurityInfo(handle, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION, ctypes.byref(owner),
                              None, None, None, ctypes.byref(sd))
    if err:
        raise OSError(errno.EACCES if err == ERROR_ACCESS_DENIED else errno.EIO, "GetSecurityInfo failed",
                      None, err)
    try:
        return _sid_string(owner)
    finally:
        k32.LocalFree(sd)


SE_FILE_OBJECT = 1
# GetFinalPathNameByHandleW's `\\?\` prefix on a local drive path.
_FINAL_PATH_PREFIX = "\\\\?\\"


def _native_file_owner(fd: int) -> str:
    """The owner SID of the file open on CRT descriptor `fd` (`GetSecurityInfo` on its handle). Raises
    `OSError`."""
    k32, adv = _win()
    handle = _msvcrt().get_osfhandle(fd)
    owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
    err = adv.GetSecurityInfo(handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION, ctypes.byref(owner),
                              None, None, None, ctypes.byref(sd))
    if err:
        raise OSError(errno.EACCES if err == ERROR_ACCESS_DENIED else errno.EIO, "GetSecurityInfo failed",
                      None, err)
    try:
        return _sid_string(owner)
    finally:
        k32.LocalFree(sd)


def _native_path_owner(path: str) -> str:
    """The owner SID of the file or directory at `path` (`GetNamedSecurityInfoW`). A final reparse point
    is followed, so a caller judging a directory resolves it first. Raises `OSError`."""
    k32, adv = _win()
    owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
    err = adv.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION, ctypes.byref(owner),
                                    None, None, None, ctypes.byref(sd))
    if err:
        raise OSError(errno.EACCES if err == ERROR_ACCESS_DENIED else errno.EIO,
                      "GetNamedSecurityInfoW failed", path, err)
    try:
        return _sid_string(owner)
    finally:
        k32.LocalFree(sd)


def _native_final_path(fd: int) -> str:
    """What the kernel says the handle behind CRT descriptor `fd` opened, every junction and link resolved
    (`GetFinalPathNameByHandleW`). Raises `OSError`."""
    k32, _ = _win()
    handle = _msvcrt().get_osfhandle(fd)
    size = 1024
    while True:
        buf = ctypes.create_unicode_buffer(size)
        got = k32.GetFinalPathNameByHandleW(handle, buf, size, 0)
        if not got:
            raise _last_error("GetFinalPathNameByHandleW")
        if got < size:
            return buf.value
        size = got + 1


def _open_process(pid: int) -> int | None:
    k32, _ = _win()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    return int(handle) if handle else None


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class TokenGroupsHeader(ctypes.Structure):
    """`TOKEN_GROUPS`' fixed part; the `SID_AND_ATTRIBUTES` array starts at `Groups`' offset."""
    _fields_ = [("GroupCount", ctypes.c_uint32), ("Groups", SidAndAttributes * 1)]


def _sid_string(psid) -> str:
    k32, adv = _win()
    text = ctypes.c_wchar_p()
    if not adv.ConvertSidToStringSidW(psid, ctypes.byref(text)):
        raise _last_error("ConvertSidToStringSidW")
    try:
        return str(text.value)
    finally:
        k32.LocalFree(text)


def _token_info(token, info_class: int):
    _k32, adv = _win()
    need = ctypes.c_ulong()
    adv.GetTokenInformation(token, info_class, None, 0, ctypes.byref(need))
    buf = ctypes.create_string_buffer(need.value or 256)
    if not adv.GetTokenInformation(token, info_class, buf, len(buf), ctypes.byref(need)):
        raise _last_error(f"GetTokenInformation({info_class})")
    return buf


def _token_sids(process: int) -> tuple[str, frozenset]:
    """`(user_sid, group_sids)` of `process`'s primary token. Raises `OSError`."""
    k32, adv = _win()
    token = ctypes.c_void_p()
    if not adv.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        raise _last_error("OpenProcessToken")
    try:
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is the PSID — a pointer INTO this
        # buffer, so it stays referenced until the SID is converted (a freed temporary made every call fail
        # with ERROR_INVALID_SID on a real PC).
        user_buf = _token_info(token, TOKEN_USER_CLASS)
        user = _sid_string(ctypes.c_void_p.from_buffer(user_buf).value)
        groups_buf = _token_info(token, TOKEN_GROUPS_CLASS)
        count = TokenGroupsHeader.from_buffer(groups_buf).GroupCount
        entries = (SidAndAttributes * count).from_buffer(groups_buf, TokenGroupsHeader.Groups.offset)
        return user, frozenset(_sid_string(entry.Sid) for entry in entries)
    finally:
        k32.CloseHandle(token)


def _native_pin_process(pid: int) -> int | None:
    """A handle on process `pid`, or None when it cannot be opened. While it is open Windows cannot reuse
    the pid. Best effort: a native process of another account usually refuses even this."""
    return _open_process(pid)


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_uint16), ("MaximumLength", ctypes.c_uint16), ("Buffer", ctypes.c_void_p)]


class _SystemProcessInformation(ctypes.Structure):
    """`SYSTEM_PROCESS_INFORMATION`'s leading fields, up to the two pids (stable since Windows XP)."""
    _fields_ = [
        ("NextEntryOffset", ctypes.c_uint32), ("NumberOfThreads", ctypes.c_uint32),
        ("WorkingSetPrivateSize", ctypes.c_int64), ("HardFaultCount", ctypes.c_uint32),
        ("NumberOfThreadsHighWatermark", ctypes.c_uint32), ("CycleTime", ctypes.c_uint64),
        ("CreateTime", ctypes.c_int64), ("UserTime", ctypes.c_int64), ("KernelTime", ctypes.c_int64),
        ("ImageName", _UnicodeString), ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]


_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


def _native_process_table() -> dict[int, tuple[int, int]]:
    """`{pid: (parent pid, creation FILETIME)}` for every process, from ONE kernel snapshot
    (`NtQuerySystemInformation(SystemProcessInformation)`). Needs no access right on any process: the
    daemon's service account can open neither a runner's token nor (for a native runner process) the
    process itself, so this is the one read of another account's process it has (measured on a real PC).
    Raises `OSError` when the snapshot fails."""
    ntdll = _win() and _NATIVE["ntdll"]
    size = ctypes.c_ulong(1 << 20)
    for _attempt in range(8):
        buf = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQuerySystemInformation(_SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(size))
        if status & 0xFFFFFFFF == _STATUS_INFO_LENGTH_MISMATCH:
            size.value += 1 << 16
            continue
        break
    if status != 0:
        raise OSError(errno.EIO, f"NtQuerySystemInformation failed (status 0x{status & 0xFFFFFFFF:08x})")
    table: dict[int, tuple[int, int]] = {}
    base, offset = ctypes.addressof(buf), 0
    while True:
        entry = _SystemProcessInformation.from_address(base + offset)
        table[int(entry.UniqueProcessId or 0)] = (int(entry.InheritedFromUniqueProcessId or 0),
                                                 int(entry.CreateTime))
        if not entry.NextEntryOffset:
            return table
        offset += entry.NextEntryOffset


def _native_creation_time(pid: int) -> int | None:
    """Process `pid`'s creation FILETIME off the kernel's process table (`_process_table`), or None."""
    entry = _process_table().get(int(pid))
    return entry[1] if entry else None


def _native_impersonated_sid(handle: int) -> str:
    """The user SID of the client connected to the pipe instance `handle`, as the kernel holds it for the
    connection (`ImpersonateNamedPipeClient` → `OpenThreadToken` → `RevertToSelf`). Needs no right on the
    client's process or token, which the daemon's account does not have. Only after the server has READ
    from the pipe (else ERROR_CANNOT_IMPERSONATE, 1368). Raises `OSError`."""
    k32, adv = _win()
    if not adv.ImpersonateNamedPipeClient(handle):
        raise _last_error("ImpersonateNamedPipeClient")
    try:
        token = ctypes.c_void_p()
        # OpenAsSelf: the access check is made against the daemon's own token, not the identification-level
        # one it is wearing (which may open nothing).
        if not adv.OpenThreadToken(k32.GetCurrentThread(), TOKEN_QUERY, True, ctypes.byref(token)):
            raise _last_error("OpenThreadToken")
        try:
            user_buf = _token_info(token, TOKEN_USER_CLASS)
            return _sid_string(ctypes.c_void_p.from_buffer(user_buf).value)
        finally:
            k32.CloseHandle(token)
    finally:
        if not adv.RevertToSelf():
            # A thread left wearing the client's identity must never serve another request.
            raise SystemExit("RevertToSelf failed: the daemon cannot shed a pipe client's identity")


_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_CONFIG = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SC_STATUS_PROCESS_INFO = 0


class _ServiceStatusProcess(ctypes.Structure):
    _fields_ = [("dwServiceType", ctypes.c_uint32), ("dwCurrentState", ctypes.c_uint32),
                ("dwControlsAccepted", ctypes.c_uint32), ("dwWin32ExitCode", ctypes.c_uint32),
                ("dwServiceSpecificExitCode", ctypes.c_uint32), ("dwCheckPoint", ctypes.c_uint32),
                ("dwWaitHint", ctypes.c_uint32), ("dwProcessId", ctypes.c_uint32),
                ("dwServiceFlags", ctypes.c_uint32)]


class _QueryServiceConfig(ctypes.Structure):
    _fields_ = [("dwServiceType", ctypes.c_uint32), ("dwStartType", ctypes.c_uint32),
                ("dwErrorControl", ctypes.c_uint32), ("lpBinaryPathName", ctypes.c_wchar_p),
                ("lpLoadOrderGroup", ctypes.c_wchar_p), ("dwTagId", ctypes.c_uint32),
                ("lpDependencies", ctypes.c_wchar_p), ("lpServiceStartName", ctypes.c_wchar_p),
                ("lpDisplayName", ctypes.c_wchar_p)]


def _native_service_process(name: str) -> tuple[int, str] | None:
    """`(pid, logon account)` of service `name` as the Service Control Manager reports them, or None when
    it is not running or cannot be asked."""
    k32, adv = _win()
    scm = adv.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if not scm:
        return None
    try:
        svc = adv.OpenServiceW(scm, name, _SERVICE_QUERY_STATUS | _SERVICE_QUERY_CONFIG)
        if not svc:
            return None
        try:
            status, need = _ServiceStatusProcess(), ctypes.c_ulong()
            if not adv.QueryServiceStatusEx(svc, _SC_STATUS_PROCESS_INFO, ctypes.byref(status),
                                            ctypes.sizeof(status), ctypes.byref(need)):
                return None
            adv.QueryServiceConfigW(svc, None, 0, ctypes.byref(need))
            buf = ctypes.create_string_buffer(max(need.value, ctypes.sizeof(_QueryServiceConfig)))
            if not adv.QueryServiceConfigW(svc, buf, len(buf), ctypes.byref(need)):
                return None
            account = _QueryServiceConfig.from_buffer(buf).lpServiceStartName or ""
            return (int(status.dwProcessId), str(account)) if status.dwProcessId else None
        finally:
            adv.CloseServiceHandle(svc)
    finally:
        adv.CloseServiceHandle(scm)


def _native_toolhelp_ppid(pid: int) -> int | None:
    """`th32ParentProcessID` of `pid` from a Toolhelp32 process snapshot, or None when `pid` is absent."""
    k32, _ = _win()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, _INVALID_HANDLE):
        return None
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        k32.CloseHandle(snap)


def _native_image_name(pid: int) -> str | None:
    """`szExeFile` (the image's file name) of `pid` from a Toolhelp32 process snapshot, or None when `pid` is
    absent. Needs no right on the process."""
    k32, _ = _win()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, _INVALID_HANDLE):
        return None
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return "".join(map(chr, entry.szExeFile)).split("\0", 1)[0]
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        k32.CloseHandle(snap)


def _native_current_sid() -> str:
    k32, _ = _win()
    return _token_sids(k32.GetCurrentProcess())[0]


def _native_lookup_account(name: str) -> tuple[str, int, str]:
    """`(sid, sid_name_use, domain)` for account `name` (`LookupAccountNameW`). Raises `OSError`."""
    k32, adv = _win()
    sid_size, dom_size, use = ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)
    adv.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None, ctypes.byref(dom_size),
                           ctypes.byref(use))
    sid = ctypes.create_string_buffer(sid_size.value or 68)
    domain = ctypes.create_unicode_buffer(dom_size.value or 256)
    if not adv.LookupAccountNameW(None, name, sid, ctypes.byref(sid_size), domain, ctypes.byref(dom_size),
                                  ctypes.byref(use)):
        raise _last_error(f"LookupAccountNameW({name})")
    return _sid_string(sid), int(use.value), str(domain.value)


def _native_system_times() -> tuple[int, int, int] | None:
    """`GetSystemTimes` → cumulative `(idle, kernel, user)` 100 ns ticks across every core (kernel
    INCLUDES idle), or None."""
    k32, _ = _win()
    idle, kernel, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
    if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return int(idle.value), int(kernel.value), int(user.value)


def _native_memory_status() -> tuple[int, int] | None:
    """`GlobalMemoryStatusEx` → `(total_phys_bytes, avail_phys_bytes)`, or None."""
    k32, _ = _win()
    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not k32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _winapi():
    import _winapi  # Windows-only stdlib module: the overlapped primitives `multiprocessing` uses.
    return _winapi


def _wait_ms(timeout: float | None) -> int:
    return INFINITE if timeout is None else max(0, int(timeout * 1000))


def _native_open_pipe(name: str, timeout: float | None) -> int:
    """A client handle on pipe `name`, waiting up to `timeout` for a free instance."""
    w = _winapi()
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            return int(w.CreateFile(name, CLIENT_PIPE_ACCESS, 0, 0, OPEN_EXISTING,
                                    FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, 0))
        except OSError as exc:
            if getattr(exc, "winerror", None) != ERROR_PIPE_BUSY:
                raise
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            raise TimeoutError(f"{name}: every pipe instance is busy")
        try:
            w.WaitNamedPipe(name, _wait_ms(left))
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_SEM_TIMEOUT:
                raise TimeoutError(f"{name}: every pipe instance is busy") from exc
            raise


ERROR_OPERATION_ABORTED = 995


def _overlapped_wait(ov, err: int, timeout: float | None) -> None:
    """Wait for an overlapped read/write; on timeout cancel it and raise `TimeoutError` — unless it
    completed in the meantime, in which case its result stands."""
    w = _winapi()
    if err == ERROR_IO_PENDING:
        if w.WaitForMultipleObjects([ov.event], False, _wait_ms(timeout)) == WAIT_TIMEOUT:
            ov.cancel()
            try:
                ov.GetOverlappedResult(True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == ERROR_OPERATION_ABORTED:
                    raise TimeoutError("named pipe I/O timed out") from exc
                raise
            return
    ov.GetOverlappedResult(True)


def _native_pipe_read(handle: int, size: int, timeout: float | None) -> bytes:
    """Up to `size` bytes; b"" once the server has closed its end (EOF)."""
    try:
        ov, err = _winapi().ReadFile(handle, size, overlapped=True)
        _overlapped_wait(ov, err, timeout)
    except BrokenPipeError:
        return b""
    except OSError as exc:
        if getattr(exc, "winerror", None) == ERROR_BROKEN_PIPE:
            return b""
        raise
    return bytes(ov.getbuffer())


def _native_pipe_write(handle: int, data: bytes, timeout: float | None) -> int:
    ov, err = _winapi().WriteFile(handle, data, overlapped=True)
    _overlapped_wait(ov, err, timeout)
    return int(ov.GetOverlappedResult(True)[0])


def _native_close_handle(handle: int) -> None:
    _winapi().CloseHandle(handle)


def _native_msvcrt():
    import msvcrt
    return msvcrt


def _native_accept_pipe(loop, pipe):
    """The proactor's own overlapped `ConnectNamedPipe` — what `start_serving_pipe` awaits per instance."""
    proactor = getattr(loop, "_proactor", None)
    if proactor is None:
        raise OSError(errno.EINVAL, "serving a named pipe needs asyncio's ProactorEventLoop")
    return proactor.accept_pipe(pipe)


def _native_make_transport(loop, pipe, protocol, extra):
    return loop._make_duplex_pipe_transport(pipe, protocol, extra=extra)


# The hooks the module calls; a test replaces these names.
_create_pipe = _native_create_pipe
_create_file = _native_create_file
_client_pid = _native_client_pid
_pipe_owner = _native_pipe_owner
_pin_process = _native_pin_process
_impersonated_sid = _native_impersonated_sid
_process_table = _native_process_table
_service_process = _native_service_process
_fleet_server_msys_pid = _native_fleet_server_msys_pid
_attested_server = _native_attested_server
_image_name = _native_image_name
_kill_fleet_server = _native_kill_fleet_server
_fleet_server_busy = _native_fleet_server_busy
_msys_winpid = _native_msys_winpid
_msys_table = _native_msys_table
_creation_time = _native_creation_time
_toolhelp_ppid = _native_toolhelp_ppid
_current_sid = _native_current_sid
_lookup_account = _native_lookup_account
_system_times = _native_system_times
_memory_status = _native_memory_status
_open_pipe = _native_open_pipe
_pipe_read = _native_pipe_read
_pipe_write = _native_pipe_write
_close_handle = _native_close_handle
_msvcrt = _native_msvcrt
_accept_pipe = _native_accept_pipe
_make_transport = _native_make_transport
_read_msys_proc = _native_read_msys_proc
_msys_proc_absent = _native_msys_proc_absent
_file_owner = _native_file_owner
_path_owner = _native_path_owner
_pane_owner_check = _native_runner_owned
_final_path = _native_final_path
_create_new_fd = _native_create_new_fd
_delete_by_handle = _native_delete_by_handle
_private_dir = _native_private_dir
_private_file = _native_private_file
_private_open = _native_private_open
_handle_facts = _native_handle_facts
_file_security = _native_file_security
_set_dacl = _native_set_dacl
_git_install_path = _native_git_install_path
_run = subprocess.run
_sleep = time.sleep


# ── the local endpoint: the broker's named pipe ─────────────────────────────────────────────────────

def pipe_sddl(daemon_sid: str, runner_sid: str, owner_sid: str) -> str:
    """The broker pipe's security descriptor: owned by `owner_sid` (the daemon process's own account,
    which a client checks), with a PROTECTED DACL (`P`: nothing inherited) admitting SYSTEM and the
    daemon's service SID with full control, the runner account with `RUNNER_PIPE_RIGHTS`, and nobody
    else. Each SID must be a literal string SID — never an alias or free text that could widen it."""
    for sid in (daemon_sid, runner_sid, owner_sid):
        if not isinstance(sid, str) or not _SID_RE.fullmatch(sid):
            raise ValueError(f"not a string SID: {sid!r}")
    return (f"O:{owner_sid}"
            f"D:P(A;;GA;;;SY)(A;;GA;;;{daemon_sid})(A;;0x{RUNNER_PIPE_RIGHTS:08x};;;{runner_sid})")


class PipeHandle:
    """One server instance — file-object-like enough for the proactor (`fileno`, `close`).

    Once connected it carries the peer as it was pinned at accept: `peer_pid`, an open `peer_process`
    handle (while it is open Windows cannot hand that pid to anyone else), and `accepted_at`, the FILETIME
    of the accept — a process created after it is a recycled pid, never the connector."""

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle
        self.accepted_at: int | None = None
        self.peer_pid: int | None = None
        self.peer_process: int | None = None

    @property
    def handle(self) -> int | None:
        return self._handle

    def fileno(self) -> int:
        if self._handle is None:
            raise ValueError("I/O operation on closed pipe")
        return self._handle

    def pin_peer(self) -> None:
        """Record the connected client off the kernel: its pid from the pipe, and — where its DACL lets the
        daemon open it — a handle on it. A runner's native process usually refuses even that; `peer_of`
        then dates it by the process table instead."""
        self.accepted_at = filetime_now()
        try:
            pid = _client_pid(self.fileno())
        except OSError as exc:
            log.warning("named pipe: could not read the connected client's pid (%s) — it is an unknown "
                        "peer", exc)
            return
        try:
            self.peer_process = _pin_process(pid)
        except OSError:
            self.peer_process = None
        self.peer_pid = pid

    def close(self) -> None:
        if self.peer_process is not None:
            process, self.peer_process = self.peer_process, None
            _close_handle(process)
        if self._handle is not None:
            handle, self._handle = self._handle, None
            _close_handle(handle)

    def __repr__(self) -> str:
        return f"<PipeHandle {self._handle!r}>"


_ACCEPT_RETRY_S = 0.1
_CREATE_ATTEMPTS = 5


class PipeServer:
    """`serve_socket`'s server: the `asyncio.AbstractServer` surface the broker uses (`close`,
    `wait_closed`, `is_serving`). At every moment it holds at least one unconnected instance — a fresh one
    is always created BEFORE the previous one is released — so the pipe name is never free for another
    process to claim. If it ever cannot create one it stops serving (`is_serving()` turns False)."""

    def __init__(self, addr: str, handler, sddl: str, limit: int) -> None:
        self.addr, self._handler, self._sddl, self._limit = addr, handler, sddl, limit
        self._loop = asyncio.get_running_loop()
        self._pending = PipeHandle(_create_pipe(addr, sddl, True))
        self._task: asyncio.Task | None = None
        self._closed = False

    def _start(self) -> None:
        self._task = self._loop.create_task(self._accept_loop())
        self._task.add_done_callback(self._on_loop_done)

    def _on_loop_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("named pipe %s: stopped serving: %s", self.addr, task.exception())

    async def _replace_pending(self) -> PipeHandle:
        """Swap in a fresh instance and return the previous one — created first, so the name stays held.

        A failed create is retried with backoff (the previous instance still holds the name meanwhile);
        after `_CREATE_ATTEMPTS` the error ends the accept loop and `is_serving()` turns False."""
        delay = _ACCEPT_RETRY_S
        for attempt in range(1, _CREATE_ATTEMPTS + 1):
            try:
                fresh = PipeHandle(_create_pipe(self.addr, self._sddl, False))
                break
            except OSError as exc:
                if attempt == _CREATE_ATTEMPTS:
                    raise
                log.warning("named pipe %s: could not open an instance (%s) — retrying", self.addr, exc)
                await asyncio.sleep(delay)
                delay *= 2
        previous, self._pending = self._pending, fresh
        return previous

    async def _accept_loop(self) -> None:
        while not self._closed:
            try:
                await _accept_pipe(self._loop, self._pending)
            except asyncio.CancelledError:
                raise
            except BrokenPipeError:
                # The client hung up between connect and accept: recycle this instance.
                (await self._replace_pending()).close()
                continue
            except OSError as exc:
                log.warning("named pipe %s: accept failed (%s) — opening a fresh instance", self.addr, exc)
                (await self._replace_pending()).close()
                await asyncio.sleep(_ACCEPT_RETRY_S)
                continue
            try:
                pipe = await self._replace_pending()
            except OSError:
                self._pending.close()   # connected but never servable: let the client see it end
                raise
            pipe.pin_peer()
            self._hand_off(pipe)

    def _hand_off(self, pipe: PipeHandle) -> None:
        reader = asyncio.StreamReader(limit=self._limit)
        protocol = asyncio.StreamReaderProtocol(reader, self._handler)
        # `socket` too: the broker reads the peer off `get_extra_info("socket")` on every OS.
        _make_transport(self._loop, pipe, protocol, {"socket": pipe, "pipe": pipe, "addr": self.addr})

    def is_serving(self) -> bool:
        return not self._closed and self._task is not None and not self._task.done()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
        self._pending.close()

    async def wait_closed(self) -> None:
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — already logged by `_on_loop_done`; shutdown must not raise
                pass


def _computer_name() -> str:
    return os.environ.get("COMPUTERNAME") or _platform.node()


def daemon_sid() -> str:
    """The daemon's service SID (`NT SERVICE\\jaahd`). Raises `OSError` when the service is not
    registered or the lookup answers with anything but a service SID."""
    sid, _use, domain = _lookup_account(f"NT SERVICE\\{DAEMON_SERVICE}")
    if domain.upper() != "NT SERVICE" or not sid.startswith(_SERVICE_SID_PREFIX):
        raise OSError(errno.EPERM, f"NT SERVICE\\{DAEMON_SERVICE} resolved to {domain}\\{sid}, not a service SID")
    return sid


def runner_sid() -> str:
    """The runner account's SID — machine-qualified, and required to be a LOCAL USER, so a domain user or
    group that happens to be called `jaah` is never the one granted the pipe. Raises `OSError`."""
    computer = _computer_name()
    sid, use, domain = _lookup_account(f"{computer}\\{RUNNER_ACCOUNT}")
    if use != SID_TYPE_USER or domain.upper() != computer.upper():
        raise OSError(errno.EPERM, f"{RUNNER_ACCOUNT} resolved to {domain} (type {use}), not a local user")
    return sid


# How the daemon's account is spelled to MSYS (and so to tmux's `server-access`): MSYS names an account
# outside the machine's own domain `<DOMAIN>+<name>`.
DAEMON_MSYS_USER = f"NT SERVICE+{DAEMON_SERVICE}"


def current_sid() -> str:
    """This process's own user SID. Raises `OSError`."""
    return _current_sid()


def is_runner_account() -> bool:
    """Whether this process runs as the runner account `jaah`. False when either SID cannot be read."""
    try:
        return current_sid() == runner_sid()
    except OSError:
        return False


async def serve_socket(addr: str, handler, *, limit: int = 2 ** 16, **_kwargs) -> PipeServer:
    """Listen on the named pipe `addr` with the broker DACL (`pipe_sddl`), calling
    `handler(reader, writer)` per connection, as `asyncio.start_unix_server` does on posix.

    Raises `OSError` when the daemon's service SID or the runner account cannot be resolved (serving
    would hide the fault), when this process runs as anything but SYSTEM or the daemon's service account
    (a client would refuse its pipe — and a daemon running as `jaah` would hand its runners full
    control), or when the name is already held — `FILE_FLAG_FIRST_PIPE_INSTANCE` refuses to join a pipe
    someone else created."""
    daemon, runner, me = daemon_sid(), runner_sid(), _current_sid()
    if me not in (SYSTEM_SID, daemon):
        raise OSError(errno.EPERM, f"the daemon runs as {me}, not SYSTEM or NT SERVICE\\{DAEMON_SERVICE}; "
                                   "refusing to serve")
    server = PipeServer(str(addr), handler, pipe_sddl(daemon, runner, me), limit)
    server._start()
    return server


class PipeClient:
    """`connect_socket`'s connection: the socket surface `agent_socket` uses (`sendall`, `recv`,
    `shutdown`, `settimeout`, `close`, a context manager)."""

    def __init__(self, handle: int, timeout: float | None) -> None:
        self._handle: int | None = handle
        self._timeout = timeout

    def fileno(self) -> int:
        if self._handle is None:
            raise OSError(errno.EBADF, "pipe is closed")
        return self._handle

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            sent = _pipe_write(self.fileno(), bytes(view), self._timeout)
            if sent <= 0:
                raise BrokenPipeError(errno.EPIPE, "named pipe accepted no bytes")
            view = view[sent:]

    def recv(self, size: int) -> bytes:
        return _pipe_read(self.fileno(), size, self._timeout)

    def shutdown(self, _how: int) -> None:
        """A no-op: a byte-mode pipe has no half-close. The request protocol is newline-framed, so the
        server never waits for EOF."""

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            _close_handle(handle)

    def __enter__(self) -> "PipeClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _verify_server(handle: int) -> None:
    """Refuse a pipe the daemon did not create: its OWNER must be SYSTEM or the daemon's service SID.
    An object's owner can only be set to the creator's own account (or a group it may own with), so a
    pipe a runner created first — squatting the name while the daemon was down — is owned by the runner
    and refused before a byte is sent. Raises `PermissionError`.

    The owner, not the server process's token: a standard-user client cannot open a SYSTEM token."""
    try:
        owner = _pipe_owner(handle)
    except OSError as exc:
        raise PermissionError(errno.EACCES, f"cannot read the pipe's owner: {exc}") from exc
    if owner != SYSTEM_SID and owner != daemon_sid():
        raise PermissionError(errno.EACCES, f"the pipe is owned by {owner}, not the {DAEMON_SERVICE} service")


def connect_socket(addr: str, timeout: float | None = None) -> PipeClient:
    """A connected client on the LOCAL named pipe `addr`, its server verified to be the daemon. The caller
    owns (and closes) it.

    Raises `OSError` (incl. `FileNotFoundError`-class errors, `TimeoutError`, and `PermissionError` for an
    impostor server) like a bare `connect`. A name outside `\\\\.\\pipe\\` is refused before any open: a
    UNC path would send the caller's credentials to another host."""
    name = str(addr)
    if not name.startswith(_PIPE_PREFIX):
        raise PermissionError(errno.EACCES, f"not a local named pipe: {name!r}")
    handle = _open_pipe(name, timeout)
    try:
        _verify_server(handle)
    except BaseException:
        _close_handle(handle)
        raise
    return PipeClient(handle, timeout)


# ── who is asking ───────────────────────────────────────────────────────────────────────────────────

def peer_of(conn) -> Peer:
    """The connecting process as the KERNEL reports it: the pid `GetNamedPipeClientProcessId` gave at
    accept, and the user SID the kernel holds for the CONNECTION, read by impersonating the client
    (`_impersonated_sid`) — never the client process's token, which the daemon's account cannot open.
    Nothing the client sent is read, but the server must have READ from the pipe first (Windows refuses
    to impersonate before), so the broker calls this after its request line. Raises `OSError` when the
    client cannot be dated or identified.

    Fails closed to an unknown peer for a connection that was never pinned (not one of `serve_socket`'s),
    and for a process created after the accept — a recycled pid, since the real connector had exited.
    The creation time is the process table's; the handle pinned at accept, where the client's DACL let
    the daemon open one, keeps the pid from being reused meanwhile."""
    pid, accepted_at = getattr(conn, "peer_pid", None), getattr(conn, "accepted_at", None)
    handle = getattr(conn, "handle", None)
    if not isinstance(pid, int) or not isinstance(accepted_at, int) or not isinstance(handle, int):
        return UNKNOWN_PEER
    created = start_time(pid)
    if created is None:
        raise OSError(errno.ESRCH, f"pipe client pid {pid} cannot be dated")
    if created > accepted_at:
        log.warning("pipe client pid %s was created after its connection was accepted — a recycled pid, "
                    "treating the peer as unknown", pid)
        return UNKNOWN_PEER
    return Peer(pid, _impersonated_sid(handle))


def start_time(pid: int) -> int | None:
    """The process's creation time as a FILETIME (`GetProcessTimes`), or None when it cannot be opened.

    Compared only against a value this same call recorded, so its unit need not match Linux's boot
    ticks: a recycled pid has a different creation time either way."""
    try:
        return _creation_time(int(pid))
    except (OSError, ValueError) as exc:
        log.debug("GetProcessTimes(%s) failed: %s", pid, exc)
        return None


# No mount namespaces here; the console names its tmux client by pid, so no stdin tty; only the Linux fleet
# server's recycle asks for children.
mount_namespace = stdin_tty = child_pids = absent


def parent_of(pid: int) -> int | None:
    """The parent pid, or None — unreadable, or a recycled pid.

    For a process Git's MSYS runtime tracks, its live MSYS parent (`_msys_hop`): MSYS `exec` replaces the
    Windows process and leaves its stub's parent dead, which on a real PC cut every pane, delegated git op
    and credential helper off from its ancestors. Otherwise the Windows parent, accepted only
    when created no later than the child — Windows keeps a parent pid after that parent exits and hands
    the pid out again, so a parent younger than its child is a stranger wearing a recycled pid.

    NOT a proof of ancestry: a creator may NAME any process it can open as the parent
    (`PROC_THREAD_ATTRIBUTE_PARENT_PROCESS`), an MSYS process writes its own parent pid, and every runner
    shares the `jaah` account. Before `pane_os_pid` lets a walk resolve a runner, the Windows-runners child
    must bind runner trees to something a child cannot forge (a Job Object per task, or an account per
    slot)."""
    hop = _msys_parent(int(pid))
    if hop is not None:
        return hop
    child_created = start_time(pid)
    try:
        ppid = _toolhelp_ppid(int(pid))
    except (OSError, ValueError) as exc:
        log.debug("Toolhelp32 ppid(%s) failed: %s", pid, exc)
        return None
    if ppid is None or child_created is None:
        return None
    # Re-read: the child itself must still be the process whose creation time was taken before the
    # snapshot, or the ppid describes whoever holds the pid now.
    if start_time(pid) != child_created:
        return None
    parent_created = start_time(ppid)
    if parent_created is None:
        return None
    if parent_created > child_created:
        log.debug("pid %s's recorded parent %s was created after it — a recycled pid, no parent", pid, ppid)
        return None
    return ppid


def _msys_parent(pid: int) -> int | None:
    """`_msys_hop` for `pid` off fresh snapshots, or None (not an MSYS-tracked process, or unreadable)."""
    try:
        msys = _msys_table_for(pid)
        table = _process_table() if msys else {}
    except OSError as exc:
        log.debug("MSYS parent of %s unreadable: %s", pid, exc)
        return None
    return _msys_hop(table, msys, pid) if pid in table else None


def _unported(name: str):
    def call(*_args, **_kwargs):
        raise NotImplementedError(f"host_platform.{name} is not implemented on Windows yet")
    call.__name__ = name
    return call


# Not in this child's scope (Architecture C3): their Windows answers belong to the runner/broker children.
owner_uid = _unported("owner_uid")
cmdline_args = _unported("cmdline_args")


def fd_path(fd: int) -> str:
    """The path the open descriptor `fd` names, as the kernel resolved it (junctions and links followed),
    without the `\\\\?\\` prefix. Raises `OSError` — never answers from a name."""
    path = _final_path(int(fd))
    if path.startswith(_FINAL_PATH_PREFIX + "UNC\\"):
        return "\\\\" + path[len(_FINAL_PATH_PREFIX) + 4:]
    if path.startswith(_FINAL_PATH_PREFIX):
        return path[len(_FINAL_PATH_PREFIX):]
    return path


def path_owner_sid(path: str) -> str:
    """The owner SID of the file or directory at `path`. Raises `OSError`."""
    owner = _path_owner(str(path))
    if not _SID_RE.fullmatch(owner or ""):
        raise OSError(errno.EIO, f"{path}'s owner did not read as a SID: {owner!r}")
    return owner


def file_owner_sid(fd: int) -> str:
    """The owner SID of the file open on `fd` — the Windows answer to posix `fstat(fd).st_uid`, which
    Windows reports as 0 for every file. Raises `OSError`."""
    owner = _file_owner(int(fd))
    if not _SID_RE.fullmatch(owner or ""):
        raise OSError(errno.EIO, f"the file's owner did not read as a SID: {owner!r}")
    return owner


# ── locks ───────────────────────────────────────────────────────────────────────────────────────────

# `msvcrt.locking` takes a C `long` byte count: the largest range it can name covers any lock file.
_LOCK_SPAN = 0x7FFFFFFF
_LOCK_POLL_S = 0.05
# What `msvcrt.locking` raises when another handle holds the range.
_HELD_ERRNOS = {errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}


def _locking(fd: int, mode: int) -> None:
    """`msvcrt.locking(fd, mode, _LOCK_SPAN)` from offset 0 — it locks from the current position — with
    the descriptor's position put back afterwards."""
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        _msvcrt().locking(fd, mode, _LOCK_SPAN)
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def lock(fh, *, exclusive: bool = True, blocking: bool = True) -> None:
    """Lock the whole file behind `fh` (a file object or a raw fd) with `msvcrt.locking`.

    Always exclusive — the CRT has no shared byte-range lock — and mandatory on Windows, which is harmless
    because every caller locks a dedicated lock file. Non-blocking and already held raises
    `BlockingIOError` (an `OSError`), exactly as the posix seam does; blocking polls `LK_NBLCK` rather
    than `LK_LOCK`, which gives up after ten seconds."""
    del exclusive
    fd = fh if isinstance(fh, int) else fh.fileno()
    ms = _msvcrt()
    while True:
        try:
            _locking(fd, ms.LK_NBLCK)
            return
        except OSError as exc:
            if exc.errno not in _HELD_ERRNOS:
                raise
            if not blocking:
                raise BlockingIOError(errno.EAGAIN, "lock is held by another handle") from exc
        _sleep(_LOCK_POLL_S)


def unlock(fh) -> None:
    """Release a lock taken by `lock`."""
    fd = fh if isinstance(fh, int) else fh.fileno()
    _locking(fd, _msvcrt().LK_UNLCK)


# ── host metrics ────────────────────────────────────────────────────────────────────────────────────

def read_cpu_totals() -> tuple | None:
    """`(total_ticks, busy_ticks)` from `GetSystemTimes`' cumulative counters, or None.

    Kernel time already includes idle, so `total = kernel + user` and `busy = total - idle`."""
    times = _system_times()
    if not times:
        return None
    idle, kernel, user = times
    total = kernel + user
    return (total, total - idle)


def read_mem() -> tuple | None:
    """`(mem_pct, mem_total_kb)` from `GlobalMemoryStatusEx`, or None. Available physical memory is
    Windows' counterpart of Linux's MemAvailable."""
    status = _memory_status()
    if not status:
        return None
    total, available = status
    if total <= 0:
        return None
    used_pct = 100.0 * (total - available) / total
    return (max(0.0, min(100.0, used_pct)), total // 1024)


def sample_metrics() -> dict:
    """One instantaneous sample. No "reboot required" flag or unattended-upgrades stamp is read here, so
    both are None — the wire carries them as None, which the CP reads as "not reported"."""
    return metrics_sample(read_cpu_totals(), read_mem(), None, None)


# ── supervision ─────────────────────────────────────────────────────────────────────────────────────

def service(name: str, *, run=None) -> Service:
    """A Windows service `name` in the SCM: `Restart-Service -Force` (a synchronous stop-then-start, which
    `sc.exe` cannot do in one call) to restart, `sc.exe query` reporting `RUNNING` for active."""
    if not _SERVICE_NAME_RE.fullmatch(name or ""):
        raise ValueError(f"not a service name: {name!r}")
    return Service(
        name,
        restart_argv=["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                      f"Restart-Service -Name '{name}' -Force -ErrorAction Stop"],
        active_argv=["sc.exe", "query", name],
        active_check=lambda proc: proc.returncode == 0 and bool(_SC_RUNNING.search(proc.stdout or "")),
        run=run or (lambda *a, **kw: _run(*a, **kw)),
    )


def own_networks():
    """Not served: no egress relay runs on Windows. The WSL2 MicroVM backend's relay unions this host's interfaces
    (`own_interfaces`, the bridge's `host` frame) into WSL's own. Raising (never an empty answer) keeps a relay that
    asks here from guarding nothing."""
    raise NotImplementedError("the node's own networks are read in the MicroVM backend, not on Windows")


_OWN_INTERFACES_SCRIPT = ("Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | "
                          "ForEach-Object { $_.IPAddress + '/' + $_.PrefixLength }")
_OWN_INTERFACES_TTL_S = 60.0
_OWN_INTERFACES: list = [None]   # (monotonic time, [cidr, ...]) of the last answer that READ something


def own_interfaces(*, run=None) -> list[str] | None:
    """This Windows' IPv4 interfaces as `addr/prefix` — the WSL NAT gateway and the LAN address included — asked
    at most once per `_OWN_INTERFACES_TTL_S`. None when PowerShell cannot answer (logged, not cached): never an
    empty list, which a guard would read as "nothing to refuse"."""
    cached = _OWN_INTERFACES[0]
    if cached is not None and time.monotonic() - cached[0] < _OWN_INTERFACES_TTL_S:
        return list(cached[1])
    try:
        proc = (run or subprocess.run)(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _OWN_INTERFACES_SCRIPT],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not list this host's IPv4 interfaces: %s", exc)
        return None
    found = [line.strip() for line in (proc.stdout or "").splitlines()
             if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}/\d{1,2}", line.strip())]
    if proc.returncode != 0 or not found:
        log.warning("could not list this host's IPv4 interfaces (exit %s)", proc.returncode)
        return None
    _OWN_INTERFACES[0] = (time.monotonic(), found)
    return list(found)
