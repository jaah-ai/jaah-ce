"""Host CPU/RAM/disk pressure and network throughput, sampled on the daemon's idle tick and posted as the observation's
`host_metrics` block. Sensing only: `apps/api/app/node_host_metrics.py` owns every threshold.

CPU: `/proc/stat` holds jiffy counters, so the delta between two reads is the EXACT mean utilisation over
that interval. Intervals are unequal (the projection pass slows under load), so `reading()` weights each
by `covered_secs`. RAM: `/proc/meminfo` is a gauge, so its window mean is a mean of instants — spikes
between ticks are invisible, an accepted approximation. `mem_pct` uses MemAvailable, never MemFree (which
would report an idle host as ~90% used). Free disk is a level, not a load: reported as the latest
reading, never windowed, since a mean would report room a clone already consumed. Network: `/proc/net/dev`
holds byte counters, so like CPU the delta is exact; rx/tx bytes/s ride the same weighted window. It is
the traffic flowing, not the link's capacity.

OS patch state (SOC2 G-35 evidence) rides the same block, latest like disk: whether Ubuntu flagged a
reboot, and when unattended-upgrades last ran. Nodes never auto-reboot (it would kill runners), so the
flag is how an operator learns a drain+reboot is due.

Load average (`/proc/loadavg`), swap (`/proc/meminfo` SwapTotal/SwapFree) and pressure stall (`/proc/pressure/*`
`some avg60`) ride latest too, each key absent when unreadable (no PSI on an older kernel). `top_processes` and
`sessions` (per-runner process-tree usage) come from `process_metrics`, a /proc walk at most once per 30s.

The window ages on `time.monotonic()`: an NTP step would freeze or empty it silently. Python 3.9, stdlib
only. Nothing here may raise into the daemon — any failure means "no reading" (`None`), logged at debug.
"""
from __future__ import annotations

import collections
import logging
import os
import shutil
import time

import environment
import host_platform
import process_metrics
from jaah_env import env_get

# The procfs readers, Linux only (`linux` imports `fcntl`, which Windows lacks).
linux_platform = host_platform.current() if host_platform.is_linux() else None

log = logging.getLogger("jaahd.host_metrics")

# Linux's sources, spelled in `host_platform/linux.py` and read at call time through these module globals
# so a test can point them at fixture files.
PROC_STAT = getattr(linux_platform, "PROC_STAT", "")
PROC_MEMINFO = getattr(linux_platform, "PROC_MEMINFO", "")
PROC_NET_DEV = getattr(linux_platform, "PROC_NET_DEV", "")
PROC_LOADAVG = getattr(linux_platform, "PROC_LOADAVG", "")
PROC_PRESSURE = getattr(linux_platform, "PROC_PRESSURE", "")
SYS_CLASS_NET = getattr(linux_platform, "SYS_CLASS_NET", "")
REBOOT_REQUIRED = getattr(linux_platform, "REBOOT_REQUIRED", "")
UPGRADE_STAMP = getattr(linux_platform, "UPGRADE_STAMP", "")

# The filesystem whose free space is reported: this node's JAAH_HOME, where the worktrees live — resolved
# per sample (a co-located node's home is under /var/lib/jaah-vn, not the host default). Set to override.
DISK_PATH = ""

# The filesystem whose inode use is reported. A tmpfs /tmp can run out of inodes with gigabytes free, and
# then every `mktemp` on the node fails; the free-space figure above never shows it.
TMP_PATH = "/tmp"

_WINDOW_ENV = "JAAHD_HOST_METRICS_WINDOW_SECS"

# Floor: `_evict` ages by interval start, so a window shorter than one sampling interval (5s loop, longer
# passes) evicts every sample on append.
_MIN_WINDOW_SECS = 30.0


def _env_float(name: str, default: float) -> float:
    """An operator float override, falling back to `default` on absent/blank/unparseable/non-positive.
    A knob with a working default must never take the daemon down."""
    raw = env_get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("host_metrics: %s=%r is not a number — using the %.1fs default", name, raw, default)
        return default
    if value <= 0:
        log.warning("host_metrics: %s=%r is not positive — using the %.1fs default", name, raw, default)
        return default
    if value < _MIN_WINDOW_SECS:
        # A floor, not a taste bound: below it the window never holds a sample and the feature silently
        # turns itself off.
        log.warning(
            "host_metrics: %s=%r is below the %.1fs floor (one sampling interval evicts itself at that "
            "width, so the window would never hold a sample) — clamping to the floor",
            name, raw, _MIN_WINDOW_SECS,
        )
        return _MIN_WINDOW_SECS
    return value


# ~60 samples at the 5s tick. The wire carries the span each reading actually covers (`window_secs`),
# so an override needs no CP change.
DEFAULT_WINDOW_SECS = _env_float(_WINDOW_ENV, 300.0)


def _read_cpu_totals() -> tuple | None:
    """`(total_jiffies, busy_jiffies)` off `PROC_STAT` — `host_platform.linux.read_cpu_totals`."""
    return linux_platform.read_cpu_totals(PROC_STAT)


def _read_mem() -> tuple | None:
    """`(mem_pct, mem_total_kb)` off `PROC_MEMINFO` — `host_platform.linux.read_mem`."""
    return linux_platform.read_mem(PROC_MEMINFO)


def _read_net_totals() -> tuple | None:
    """`(rx_bytes, tx_bytes)` off `PROC_NET_DEV` — `host_platform.linux.read_net_totals`."""
    return linux_platform.read_net_totals(PROC_NET_DEV, SYS_CLASS_NET)


def _read_disk(path: str) -> tuple | None:
    """`(free_kb, total_kb)` for the filesystem holding `path`, or None when it cannot be read.

    `f_frsize` is the unit `f_blocks`/`f_bavail` count in (`f_bsize` differs on some filesystems).
    `f_bavail`, never `f_bfree`: the root-reserved margin is unallocatable by an unprivileged user.

    `path` itself or nothing — never walk up to an existing ancestor. If `DISK_PATH` is an unmounted
    volume, a walk would report `/`'s free space and feed the placement reserve a fail-open "there is
    room". A missing path is None, which every consumer treats as not-ready. kB, matching `mem_total_kb`."""
    if not hasattr(os, "statvfs"):
        # Windows has no `statvfs`; `disk_usage` reads GetDiskFreeSpaceEx, whose `free` is already the
        # space available to the caller (the `f_bavail` analogue).
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            log.debug("host_metrics: cannot read disk usage of %s: %s", path, exc)
            return None
        if usage.total <= 0:
            return None
        return (max(0, usage.free // 1024), usage.total // 1024)
    try:
        st = os.statvfs(path)
    except OSError as exc:
        # Normal on an unprovisioned node.
        log.debug("host_metrics: cannot statvfs %s: %s", path, exc)
        return None
    unit = st.f_frsize or st.f_bsize
    if unit <= 0 or st.f_blocks <= 0:
        # No blocks is not a measurement, and a zero total would divide by zero at every reader.
        log.debug("host_metrics: %s reports frsize=%r blocks=%r — no usable figure",
                  path, st.f_frsize, st.f_blocks)
        return None
    free_kb = (st.f_bavail * unit) // 1024
    total_kb = (st.f_blocks * unit) // 1024
    return (max(0, free_kb), total_kb)


def _read_inodes(path: str) -> tuple | None:
    """`(free, total)` inodes of the filesystem holding `path`, or None when it cannot be read.

    None on Windows (no `statvfs`), on a statvfs error, and for `f_files == 0` — filesystems that
    allocate inodes dynamically (btrfs, some overlays) report no fixed table, which is not "all used"."""
    if not hasattr(os, "statvfs"):
        return None
    try:
        st = os.statvfs(path)
    except OSError as exc:
        log.debug("host_metrics: cannot statvfs %s for inodes: %s", path, exc)
        return None
    if st.f_files <= 0:
        return None
    return (max(0, min(st.f_ffree, st.f_files)), st.f_files)


def _read_signals() -> dict:
    """The latest load, swap and PSI figures (Linux only; `{}` elsewhere). Unreadable ones are left out."""
    platform = linux_platform
    if platform is None:
        return {}
    out: dict = {}
    load = platform.read_loadavg(PROC_LOADAVG)
    if load is not None:
        out["load_1"], out["load_5"], out["load_15"] = load
    swap = platform.read_swap(PROC_MEMINFO)
    if swap is not None:
        out["swap_total_kb"], out["swap_free_kb"] = swap
    for key, resource in (("psi_cpu_pct", "cpu"), ("psi_mem_pct", "memory"), ("psi_io_pct", "io")):
        pct = platform.read_psi(PROC_PRESSURE.format(resource))
        if pct is not None:
            out[key] = pct
    return out


def _read_patch_state() -> tuple:
    """`(reboot_required, last_upgrade_epoch)` — `host_platform.linux.read_patch_state`."""
    return linux_platform.read_patch_state(REBOOT_REQUIRED, UPGRADE_STAMP)


def _raw_sample() -> dict:
    """One instantaneous sample (`host_platform._types.metrics_sample`'s shape). On Linux it is read
    through this module's own readers, so the fixture redirection above keeps working; elsewhere the
    seam answers."""
    if host_platform.is_linux():
        reboot, stamp = _read_patch_state()
        return {"cpu_totals": _read_cpu_totals(), "mem": _read_mem(),
                "reboot_required": reboot, "last_upgrade_epoch": stamp, "net_totals": _read_net_totals()}
    return host_platform.sample_metrics()


class HostMetricsSampler:
    """The rolling window behind one node's `host_metrics` block. Long-lived, one per daemon.

    The CPU figure is a delta against the previous read, so a per-pass instance would never produce one;
    `main.NodeStatePublisher` holds it, not the per-tick `ReconcileContext`. `sample()` is cheap and safe
    on every ~5s tick; `reading()` is pure. Neither raises."""

    def __init__(self, window_secs: float = DEFAULT_WINDOW_SECS, clock=time.monotonic) -> None:
        self._window_secs = float(window_secs)
        self._clock = clock
        # (monotonic_ts, covered_secs, cpu_pct, mem_pct, net) per completed sample, oldest first; `net` is
        # `(rx_bps, tx_bps, covered_secs)` or None when no network delta was usable that tick.
        self._window = collections.deque()
        # None ⇒ no baseline yet; the next `sample()` establishes one and contributes nothing.
        self._prev_cpu = None
        self._prev_cpu_at = None
        # The network counters' own baseline: their reads fail independently of /proc/stat's.
        self._prev_net = None
        self._prev_net_at = None
        # Refreshed from the last successful read, so the wire never carries an unobserved value.
        self._mem_total_kb = None
        self._cpu_count = os.cpu_count()
        # Latest, not windowed; folded like `_mem_total_kb`.
        self._disk_free_kb = None
        self._disk_total_kb = None
        # Latest, like disk.
        self._tmp_inodes_free = None
        self._tmp_inodes_total = None
        # Latest, like disk; refreshed every tick so a reboot clears the flag within one tick.
        self._reboot_required = None
        self._last_upgrade_epoch = None
        # Latest, overwritten every tick: load/swap/PSI keys present only when read.
        self._signals: dict = {}
        # The /proc walk behind `top_processes`/`sessions`; it throttles itself to one walk per 30s.
        self._processes = process_metrics.ProcessSampler(clock=clock)

    def sample(self) -> None:
        """Take one sample. Never raises.

        Whenever `/proc/stat` reads, the baseline advances — even if the sample was unusable — so the
        next delta never spans two intervals weighted as one. A failed read leaves the baseline, and the
        next delta's `covered_secs` records the whole outage. No entry is recorded on the first call (no
        baseline, so `reading()` is None for one tick) or when `total_delta <= 0` (a reset or suspended
        container — untrustworthy).
        """
        try:
            self._sample()
        except Exception as exc:   # noqa: BLE001 — one optional wire field may never break the pass
            log.debug("host_metrics: sample failed, no reading this tick: %s", exc)

    def _sample(self) -> None:
        now = self._clock()
        raw = _raw_sample()
        cpu = raw["cpu_totals"]
        mem = raw["mem"]

        prev, prev_at = self._prev_cpu, self._prev_cpu_at
        if cpu is not None:
            self._prev_cpu, self._prev_cpu_at = cpu, now

        cpu_pct = None
        covered = None
        if cpu is not None and prev is not None and prev_at is not None:
            total_delta = cpu[0] - prev[0]
            busy_delta = cpu[1] - prev[1]
            if total_delta > 0:
                # Clamped: per-state counters read mid-update can land marginally outside 0..total,
                # and the CP rejects a block outside 0..100.
                cpu_pct = max(0.0, min(100.0, 100.0 * busy_delta / total_delta))
                covered = max(0.0, now - prev_at)

        net = self._net_rate(raw.get("net_totals"), now)

        if mem is not None:
            self._mem_total_kb = mem[1]

        # Outside the usability gate and the window: a tick with no CPU delta still read the disk.
        disk = _read_disk(DISK_PATH or environment.pool_root_or_empty() or host_platform.paths().home_root)
        if disk is not None:
            self._disk_free_kb, self._disk_total_kb = disk
        inodes = _read_inodes(TMP_PATH)
        if inodes is not None:
            self._tmp_inodes_free, self._tmp_inodes_total = inodes
        # Unlike disk, overwritten even when unreadable: a stale "reboot required" is a false claim.
        self._reboot_required, self._last_upgrade_epoch = raw["reboot_required"], raw["last_upgrade_epoch"]
        self._signals = _read_signals()
        self._processes.sample()

        # Aged first and unconditionally, before the usability gate: otherwise a sensing outage keeps
        # re-posting the last window forever, and the CP (which dates a reading by receipt) never sees
        # it go stale. Aging here empties the window after `window_secs`, so the wire key is omitted.
        self._evict(now)

        if cpu_pct is None or covered is None or mem is None:
            return
        self._window.append((now, covered, cpu_pct, mem[0], net))

    def _net_rate(self, totals, now: float) -> tuple | None:
        """`(rx_bps, tx_bps, covered_secs)` since the previous read, or None — advancing the baseline the
        way `_sample` does for CPU. A counter that went backwards (an interface removed or reset) is no
        rate rather than a negative one."""
        prev, prev_at = self._prev_net, self._prev_net_at
        if totals is None:
            return None
        self._prev_net, self._prev_net_at = totals, now
        if prev is None or prev_at is None:
            return None
        elapsed = now - prev_at
        rx, tx = totals[0] - prev[0], totals[1] - prev[1]
        if elapsed <= 0 or rx < 0 or tx < 0:
            return None
        return (rx / elapsed, tx / elapsed, elapsed)

    def _evict(self, now: float) -> None:
        """Drop entries whose measured interval BEGAN before the window opened.

        Aged by the interval's start (`ts - covered_secs`), so coverage matches `window_secs` exactly.
        The deque is append-ordered by a monotonic clock, so only the left end is ever dropped."""
        cutoff = now - self._window_secs
        while self._window and (self._window[0][0] - self._window[0][1]) < cutoff:
            self._window.popleft()

    def reading(self) -> dict | None:
        """This node's `host_metrics` wire block, or None when there is nothing to report.

        None, not a zeroed block: the caller omits the key, and an unreadable /proc is not an idle host.

        `window_secs` is measured from the start of the oldest sample's own interval, so a single sample
        covers one interval rather than 0.0 (which the CP rejects). The mean is time-weighted by
        `covered_secs`: intervals stretch under load, and an unweighted mean would under-weight the busy
        ones by up to 3×. RAM rides the same weights."""
        if not self._window:
            return None
        weight = sum(entry[1] for entry in self._window)
        if weight > 0:
            cpu_mean = sum(entry[2] * entry[1] for entry in self._window) / weight
            mem_mean = sum(entry[3] * entry[1] for entry in self._window) / weight
        else:
            # Only reachable from an injected clock that does not advance; equal weights, no raise.
            cpu_mean = sum(entry[2] for entry in self._window) / len(self._window)
            mem_mean = sum(entry[3] for entry in self._window) / len(self._window)
        net_entries = [entry[4] for entry in self._window if entry[4] is not None]
        net_weight = sum(n[2] for n in net_entries)
        rx_bps = tx_bps = None
        if net_entries and net_weight > 0:
            rx_bps = round(sum(n[0] * n[2] for n in net_entries) / net_weight, 1)
            tx_bps = round(sum(n[1] * n[2] for n in net_entries) / net_weight, 1)
        newest, oldest = self._window[-1], self._window[0]
        window_secs = (newest[0] - oldest[0]) + oldest[1]
        block = {
            "cpu_pct": round(cpu_mean, 2),
            "mem_pct": round(mem_mean, 2),
            "cpu_count": self._cpu_count,
            "mem_total_kb": self._mem_total_kb,
            "window_secs": round(window_secs, 1),
            "samples": len(self._window),
            # Latest, emitted even as None: the CP reads absent as "no disk reading" (fail closed),
            # where an omitted key would look like an older daemon.
            "disk_free_kb": self._disk_free_kb,
            "disk_total_kb": self._disk_total_kb,
            # Latest, like disk: /tmp's free and total inodes.
            "tmp_inodes_free": self._tmp_inodes_free,
            "tmp_inodes_total": self._tmp_inodes_total,
            "reboot_required": self._reboot_required,
            "last_upgrade_epoch": self._last_upgrade_epoch,
            # Windowed like CPU, bytes per second; None where this OS has no reader or no delta landed.
            "net_rx_bps": rx_bps,
            "net_tx_bps": tx_bps,
        }
        # Latest, not windowed, and absent (never None) when unread: older daemons send none of them.
        block.update(self._signals)
        block.update(self._processes.reading() or {})
        return block
