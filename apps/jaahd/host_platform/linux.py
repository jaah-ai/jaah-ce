"""Linux: the daemon's original host code, MOVED here unchanged — procfs, `SO_PEERCRED`, systemd.

Every literal below is the value the packaged node has always used; the seam changes where it is
spelled, never what it is (`tests/test_host_platform.py` pins the layout)."""
from __future__ import annotations

import dataclasses
import functools
import ipaddress
import logging
import os
import platform as _platform
import posixpath
import re
import socket
import stat
import struct
import subprocess
import sys
from pathlib import Path

from host_platform import _posix
from host_platform._types import Paths, Peer, Service, metrics_sample

OS = "linux"

_RUN_DIR = "/run/jaah"


def _under_run(*parts: str) -> str:
    """A path under the runtime tree, derived (never spelled whole) so `environment.run_path` stays the one
    place a relocated node's `$JAAH_RUN_ROOT` applies."""
    return posixpath.join(_RUN_DIR, *parts)


# The node's own interpreter: the `jaah-python` package, a pinned CPython the
# `jaah` .deb depends on, so the daemon never runs on whatever the distro ships (3.10 on Ubuntu 22.04). The distro's
# `/usr/bin/python3` is left alone, and never first on a runner's PATH: daemon-built commands spell this path.
JAAH_PYTHON = "/usr/lib/jaah-python/bin/python3"

_PATHS = Paths(
    etc_dir="/etc/jaah",
    run_dir=_RUN_DIR,
    creds_socket=_under_run("creds", "sock"),
    state_dir="/var/lib/jaahd",
    bundle_root="/usr/lib/jaahd",
    lib_root="/usr/lib/jaahd",
    python=JAAH_PYTHON,
    home_root="/home/jaah/.jaah",
    docker_host="",
    tmux=_under_run("tmux"),
)

# procfs paths `host_metrics` reads, named so a test can point them at fixture files.
PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"
PROC_NET_DEV = "/proc/net/dev"
PROC_LOADAVG = "/proc/loadavg"
# The kernel's IPv4 tables the MicroVM egress relay reads for the node's own addresses and networks.
PROC_NET_FIB_TRIE = "/proc/net/fib_trie"
PROC_NET_ROUTE = "/proc/net/route"
# Pressure stall information (Linux >= 4.20 with CONFIG_PSI); `{}` is `cpu`, `memory` or `io`.
PROC_PRESSURE = "/proc/pressure/{}"
# Per-interface IPv6 knobs (the MicroVM tap pool turns IPv6 off on its taps).
PROC_SYS_IPV6_CONF = "/proc/sys/net/ipv6/conf"
# One directory per interface; a `device` link inside marks a NIC backed by hardware (or a hypervisor's).
SYS_CLASS_NET = "/sys/class/net"
# Written by a package upgrade that needs a reboot; removed by the reboot.
REBOOT_REQUIRED = "/var/run/reboot-required"
# Touched by apt.systemd.daily after each successful unattended-upgrades run (and by the bootstrap's own).
UPGRADE_STAMP = "/var/lib/apt/periodic/upgrade-stamp"

_metrics_log = logging.getLogger("jaahd.host_metrics")

lock = _posix.lock
unlock = _posix.unlock
serve_socket = _posix.serve_socket
connect_socket = _posix.connect_socket
pane_os_pid = _posix.pane_os_pid


path_key = _posix.path_key
is_under = _posix.is_under


def machine() -> str:
    return _platform.machine()


def os_version() -> str:
    """The kernel release (`uname -r`) — what an operator matches a Linux node's patch level against."""
    return _platform.release()


def paths() -> Paths:
    """The packaged layout, or a community-edition node's (`_ce_paths`). The edition is read at call time
    (`apps/jaahd/edition.py`)."""
    import edition  # noqa: PLC0415 — a stdlib-only leaf beside this package

    return _ce_paths() if edition.IS_CE else _PATHS


@functools.cache
def _ce_paths() -> Paths:
    """A community-edition node's layout: the packaged one, except that its bundle is wherever the installer staged
    it — no root there, so `jaah-ce-node` puts it under the user's data dir (the container keeps the package
    path) — and it runs under the Python running this daemon. The root is the directory holding `apps/`, three
    levels above this package, resolved as `agents.base.hub_root` resolves it."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
    return dataclasses.replace(_PATHS, bundle_root=root, lib_root=root, python=sys.executable)


# ── what the machine is ─────────────────────────────────────────────────────────────────────────────

# Opened by the kvm module when the CPU and its hypervisor expose virtualization extensions.
DEV_KVM = "/dev/kvm"
# One lowercase token (`none`, `kvm`, `amazon`…); anything else is not systemd's answer.
_VIRT_TOKEN = re.compile(r"[a-z0-9_-]{1,32}")


def virtualization(*, run=subprocess.run) -> str | None:
    """`systemd-detect-virt --vm`'s answer — `none` on bare metal, else the hypervisor — or None when the tool
    is missing, hangs or says something unrecognisable. `--vm` looks through a container to the machine under
    it, so a daemon in Docker never calls its host a VM. It exits 1 when it prints `none`, so the output is read
    whatever the exit code."""
    try:
        out = run(["systemd-detect-virt", "--vm"], capture_output=True, text=True, timeout=2, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    value = (out or "").strip().lower()
    return value if _VIRT_TOKEN.fullmatch(value) else None


def kvm(path: str = DEV_KVM) -> bool:
    """Whether `/dev/kvm` exists as a character device — the host CAN run KVM guests. Existence, not
    `os.access`: the daemon's and runner's accounts may lack the `kvm` group, and that is a grant to fix,
    not a property of the machine."""
    try:
        return stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


def hypervisor() -> str:
    return "kvm"


def microvm_prereq() -> None:
    """Never sent: a Linux node's unmet prerequisite is `kvm` False, which the CP words itself (`NO_KVM`)."""
    return None


# Which release this is: `ID` and `VERSION_ID`, as the CP's supported list (`packaging/linux-support.toml`) keys it.
OS_RELEASE = "/etc/os-release"
# The EFS mount helper and, where the package ships it, the TLS proxy it starts — at the package's own path
# (`dpkg -c`: usr/bin/efs-proxy). The proxy has no `--version` (clap exits 2), so it is run with `--help`.
MOUNT_EFS = "/sbin/mount.efs"
EFS_PROXY = "/usr/bin/efs-proxy"
# Written by the toolchain layer only after `docker info` as the session user showed a ROOTLESS engine, removed on
# any run that could not show it (`infra/jaah/node_reprovision.py::record_rootless_docker`, whose constant this
# mirrors — no import across the two; a test pins them). Never inferred from the tools being installed.
ROOTLESS_DOCKER_MARKER = "/var/lib/jaahd/rootless-docker.ok"
_OS_RELEASE_LINE = re.compile(r"""([A-Z_]+)=(?:"(.*)"|'(.*)'|(.*))""")


def distro(path: str = OS_RELEASE) -> dict | None:
    """`{"id": ID, "version_id": VERSION_ID}` from os-release (`version_id` left out when the file has none), or None
    when it is unreadable or names no `ID`. The CP bounds both (`fleet_observe`)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    fields = {}
    for line in lines:
        match = _OS_RELEASE_LINE.fullmatch(line.strip())
        if match:
            fields[match.group(1)] = next(g for g in match.groups()[1:] if g is not None)
    if not fields.get("ID"):
        return None
    return {"id": fields["ID"], **({"version_id": fields["VERSION_ID"]} if fields.get("VERSION_ID") else {})}


def _runs(binary: str, flag: str = "--version", *, run=subprocess.run) -> bool:
    """Whether `<binary> <flag>` exits 0 — running it, so a binary built against a newer glibc reads as absent."""
    try:
        return run([binary, flag], capture_output=True, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def host_features(*, run=subprocess.run, exists=os.path.exists) -> dict:
    """The probed host features the CP's capability rules read (`node_platform.FEATURE_VALUES`). Each probe this
    release ships always emits its key, true or false; one it does not ship is absent (= not probed).

    `mount_efs`: the EFS mount helper runs, and so does its TLS proxy where the package installs one.
    `rootless_docker`: ROOTLESS_DOCKER_MARKER exists — the session user's rootless engine was verified."""
    mount_efs = _runs(MOUNT_EFS, run=run) and (not exists(EFS_PROXY) or _runs(EFS_PROXY, "--help", run=run))
    return {"mount_efs": mount_efs, "rootless_docker": bool(exists(ROOTLESS_DOCKER_MARKER))}


# ── who is asking ───────────────────────────────────────────────────────────────────────────────────

def peer_of(conn) -> Peer:
    """`SO_PEERCRED` on a connected AF_UNIX socket: (pid, uid) stamped by the kernel at `connect()`.

    `struct ucred` is three 32-bit ints ("3i") on every Linux ABI here. Raises `OSError` when the
    kernel cannot be asked; the caller decides what an unknown peer means."""
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", raw)
    except struct.error as exc:
        raise OSError(f"unparseable SO_PEERCRED reply: {exc}") from exc
    return Peer(pid, str(uid))


def _stat_fields(pid: int) -> list | None:
    """`/proc/<pid>/stat` split after the LAST `)` (`comm` can contain spaces and parentheses), or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
        return raw.rsplit(b")", 1)[1].split()
    except (OSError, IndexError):
        return None


def start_time(pid: int) -> int | None:
    """Field 22 of `/proc/<pid>/stat` (start time in ticks since boot), or None when unreadable."""
    fields = _stat_fields(pid)
    try:
        return None if fields is None else int(fields[19])
    except (ValueError, IndexError):
        return None


def parent_of(pid: int) -> int | None:
    """PPid — field 4 of `/proc/<pid>/stat` — or None when unreadable. A targeted read, not a /proc scan."""
    fields = _stat_fields(pid)
    try:
        return None if fields is None else int(fields[1])
    except (ValueError, IndexError):
        return None


def cmdline_args(pid: int) -> list | None:
    """`/proc/<pid>/cmdline` as its non-empty argv elements (bytes), or None when there is no entry.
    An empty list is a kernel thread or a zombie."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return [arg for arg in raw.split(b"\0") if arg]


def mount_namespace(pid: "int | str") -> str | None:
    """`/proc/<pid>/ns/mnt`'s link text (`"self"` for this process), or None when unreadable."""
    try:
        return os.readlink(f"/proc/{pid}/ns/mnt")
    except OSError:
        return None


def stdin_tty(pid: "int | str") -> str | None:
    """The terminal `pid`'s stdin names (`/proc/<pid>/fd/0`'s link text), or None when unreadable."""
    try:
        return os.readlink(f"/proc/{pid}/fd/0")
    except OSError:
        return None


def child_pids(pid: "int | str") -> set | None:
    """`pid`'s direct children as pid strings (`/proc/<pid>/task/<pid>/children`), or None when unreadable."""
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="ascii") as fh:
            return set(fh.read().split())
    except OSError:
        return None


def owner_uid(pid: int) -> int | None:
    """The real uid owning `pid` (the owner of `/proc/<pid>`), or None when it cannot be read."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def fd_path(fd: int) -> str:
    """What the KERNEL says this descriptor opened (`/proc/self/fd/<fd>`). Raises `OSError` when /proc
    is unavailable — callers deny on that."""
    return os.readlink(f"/proc/self/fd/{fd}")


# ── host metrics ────────────────────────────────────────────────────────────────────────────────────

def read_cpu_totals(path: str = PROC_STAT) -> tuple | None:
    """`(total_jiffies, busy_jiffies)` off `/proc/stat`'s aggregate `cpu` line, or None when unreadable.

    The line is::

        cpu  user nice system idle iowait irq softirq steal guest guest_nice
              [0]  [1]   [2]  [3]    [4]  [5]     [6]   [7]    [8]       [9]

    `total` sums the first eight fields: `guest`/`guest_nice` are already counted inside `user`/`nice`.
    `busy = total - idle - iowait` — a core waiting on disk is available, so an IO-bound git checkout
    must not read as CPU-saturated. The aggregate line spans all cores, so the ratio is already 0..100
    for the whole host."""
    try:
        with open(path) as fh:
            first = fh.readline()
    except OSError as exc:
        _metrics_log.debug("host_metrics: cannot read %s: %s", path, exc)
        return None
    fields = first.split()
    # A masked or synthesised /proc can hand back something that is not this line at all.
    if not fields or fields[0] != "cpu" or len(fields) < 9:
        _metrics_log.debug("host_metrics: %s's first line is not the aggregate cpu line: %r", path, first)
        return None
    try:
        values = [int(v) for v in fields[1:9]]
    except ValueError as exc:
        _metrics_log.debug("host_metrics: unparseable jiffy counters in %s: %s", path, exc)
        return None
    total = sum(values)
    busy = total - values[3] - values[4]   # minus idle, minus iowait
    return (total, busy)


def read_mem(path: str = PROC_MEMINFO) -> tuple | None:
    """`(mem_pct, mem_total_kb)` off `/proc/meminfo`, or None when unreadable or implausible.

    MemAvailable (Linux >= 3.14) missing yields None rather than a fall back to MemFree: a
    plausible-looking wrong number is worse than a missing one. A gauge, so one read is complete."""
    wanted = ("MemTotal:", "MemAvailable:")
    found = {}
    try:
        with open(path) as fh:
            for line in fh:
                key = line.split(None, 1)[0] if line.strip() else ""
                if key in wanted:
                    found[key] = int(line.split()[1])
                    if len(found) == len(wanted):
                        break
    except (OSError, ValueError, IndexError) as exc:
        _metrics_log.debug("host_metrics: cannot read %s: %s", path, exc)
        return None
    total = found.get("MemTotal:")
    available = found.get("MemAvailable:")
    if total is None or available is None:
        _metrics_log.debug("host_metrics: %s carried no MemTotal/MemAvailable pair: %r", path, found)
        return None
    if total <= 0:
        _metrics_log.debug("host_metrics: %s reports MemTotal=%r — no usable ratio", path, total)
        return None
    # Clamped: MemAvailable is an estimate and may exceed MemTotal.
    used_pct = 100.0 * (total - available) / total
    return (max(0.0, min(100.0, used_pct)), total)


def read_net_totals(path: str = PROC_NET_DEV, sys_class_net: str = SYS_CLASS_NET) -> tuple | None:
    """`(rx_bytes, tx_bytes)` summed over this host's device-backed NICs, or None when unreadable.

    Device-backed only (a `device` entry under `sys_class_net/<iface>`): `lo`, docker bridges, `veth`
    pairs and `tailscale0` carry traffic that also crosses the physical NIC, so summing them would count
    one byte two or three times. Where no interface has a device (a container's view), every non-`lo`
    interface counts instead — a figure that may double count beats none. Cumulative counters, like
    `read_cpu_totals`: the rate is a delta, taken by the sampler."""
    try:
        with open(path) as fh:
            lines = fh.readlines()[2:]   # two header lines
    except OSError as exc:
        _metrics_log.debug("host_metrics: cannot read %s: %s", path, exc)
        return None
    counters = {}
    for line in lines:
        name, sep, rest = line.partition(":")
        fields = rest.split()
        if not sep or len(fields) < 9:
            continue
        try:
            # Receive bytes is field 0, transmit bytes field 8.
            counters[name.strip()] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    counters.pop("lo", None)
    if not counters:
        _metrics_log.debug("host_metrics: %s lists no non-loopback interface", path)
        return None
    physical = {n: c for n, c in counters.items() if os.path.exists(os.path.join(sys_class_net, n, "device"))}
    chosen = physical or counters
    return (sum(c[0] for c in chosen.values()), sum(c[1] for c in chosen.values()))


def read_loadavg(path: str = PROC_LOADAVG) -> tuple | None:
    """`(load_1, load_5, load_15)` off `/proc/loadavg`, or None when unreadable or implausible."""
    try:
        with open(path) as fh:
            fields = fh.read().split()
        values = tuple(float(v) for v in fields[:3])
    except (OSError, ValueError) as exc:
        _metrics_log.debug("host_metrics: cannot read %s: %s", path, exc)
        return None
    if len(values) != 3 or any(not (v >= 0.0) or v == float("inf") for v in values):
        _metrics_log.debug("host_metrics: %s carried no usable load triple: %r", path, values)
        return None
    return values


def read_swap(path: str = PROC_MEMINFO) -> tuple | None:
    """`(swap_total_kb, swap_free_kb)` off `/proc/meminfo`, or None when either is missing. A host with no
    swap reads `(0, 0)`, which is a fact, not a gap."""
    found = {}
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in ("SwapTotal:", "SwapFree:"):
                    found[parts[0]] = int(parts[1])
    except (OSError, ValueError) as exc:
        _metrics_log.debug("host_metrics: cannot read %s: %s", path, exc)
        return None
    total, free = found.get("SwapTotal:"), found.get("SwapFree:")
    if total is None or free is None or total < 0 or free < 0:
        return None
    return (total, min(free, total))


def read_psi(path: str) -> float | None:
    """The `some avg60` figure (percent of the last minute some task stalled) off one `/proc/pressure/*`
    file, or None when the file is missing (no PSI on this kernel) or unparseable."""
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0] == "some":
                    for part in parts[1:]:
                        key, _, value = part.partition("=")
                        if key == "avg60":
                            pct = float(value)
                            return pct if 0.0 <= pct <= 100.0 else None
    except (OSError, ValueError) as exc:
        _metrics_log.debug("host_metrics: cannot read %s: %s", path, exc)
    return None


def read_patch_state(reboot_required: str = REBOOT_REQUIRED,
                     upgrade_stamp: str = UPGRADE_STAMP) -> tuple:
    """`(reboot_required, last_upgrade_epoch)`. The epoch is None when the stamp cannot be read.

    A stamp never written is None, not 0: "never upgraded" and "cannot tell" look alike from here, and
    the CP must read neither as a date."""
    # `exists` folds an unreadable path into False; the flag lives in a world-readable directory.
    reboot = os.path.exists(reboot_required)
    try:
        stamp = int(os.stat(upgrade_stamp).st_mtime)
    except (OSError, ValueError, OverflowError) as exc:
        # Normal on a node without unattended-upgrades.
        _metrics_log.debug("host_metrics: cannot stat %s: %s", upgrade_stamp, exc)
        stamp = None
    return (reboot, stamp)


def sample_metrics() -> dict:
    """One instantaneous sample off procfs and apt's stamps (`_types.metrics_sample`'s shape)."""
    reboot, stamp = read_patch_state()
    return metrics_sample(read_cpu_totals(), read_mem(), reboot, stamp, read_net_totals())


# ── supervision ─────────────────────────────────────────────────────────────────────────────────────

def service(name: str, *, run=None) -> Service:
    """A systemd unit: `systemctl restart <name>` / `systemctl is-active --quiet <name>`."""
    return Service(name, restart_argv=["systemctl", "restart", name],
                   active_argv=["systemctl", "is-active", "--quiet", name], run=run)


def local_ipv4(fib_trie: "str | Path" = PROC_NET_FIB_TRIE) -> set[str]:
    """Every IPv4 address local to this netns (the `/32 host LOCAL` leaves of the FIB)."""
    found: set[str] = set()
    try:
        lines = Path(fib_trie).read_text().splitlines()
    except OSError:
        return found
    last = None
    for line in lines:
        text = line.strip()
        if text.startswith("|--"):
            last = text[3:].strip()
        elif text == "/32 host LOCAL" and last:
            found.add(last)
    return found


def connected_networks(route: "str | Path" = PROC_NET_ROUTE) -> list[ipaddress.IPv4Network]:
    """Every non-default IPv4 route's destination: the networks this node is attached to or routes to."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        lines = Path(route).read_text().splitlines()[1:]
    except OSError:
        return nets
    for line in lines:
        cols = line.split()
        if len(cols) < 8:
            continue
        try:
            dest = socket.inet_ntoa(struct.pack("<I", int(cols[1], 16)))
            mask = socket.inet_ntoa(struct.pack("<I", int(cols[7], 16)))
            net = ipaddress.IPv4Network(f"{dest}/{mask}", strict=False)
        except (ValueError, OSError):
            continue
        if net.prefixlen:
            nets.append(net)
    return nets


#: Interfaces of the machine this kernel runs INSIDE, outside its own view: a Windows MicroVM node's backend runs
#: in a WSL2 distro, and the Windows host's addresses reach it in the bridge's `host` frame (`microvm_backend`).
_OUTER_INTERFACES: list[ipaddress.IPv4Interface] = []


def set_outer_interfaces(cidrs: list[str]) -> None:
    """Replace `_OUTER_INTERFACES` with each parseable IPv4 `addr/prefix` in CIDRS (anything else is dropped)."""
    parsed = []
    for cidr in cidrs:
        try:
            parsed.append(ipaddress.IPv4Interface(str(cidr)))
        except ValueError:
            continue
    _OUTER_INTERFACES[:] = parsed


def own_networks(fib_trie: "str | Path" = PROC_NET_FIB_TRIE,
                 route: "str | Path" = PROC_NET_ROUTE) -> tuple[set[str], list[ipaddress.IPv4Network]]:
    """`(local_ipv4(), connected_networks())`: the node's own IPv4 addresses and the networks it reaches
    directly — what the MicroVM egress relay never lets a guest dial — unioned with `_OUTER_INTERFACES`."""
    addrs, nets = local_ipv4(fib_trie), connected_networks(route)
    for iface in _OUTER_INTERFACES:
        addrs.add(str(iface.ip))
        if iface.network.prefixlen and iface.network not in nets:
            nets.append(iface.network)
    return addrs, nets
