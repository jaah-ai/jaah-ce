"""The seam's value types and the pieces every implementation shares. Stdlib only, no side effects."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, NamedTuple, Sequence

# `platform.machine()` spellings → the two architectures a node reports. Anything else passes through
# lower-cased: an unknown arch is reported as itself rather than guessed at.
_ARCH_ALIASES = {
    "x86_64": "x64", "amd64": "x64",
    "aarch64": "arm64", "arm64": "arm64",
}


def normalise_arch(machine: str) -> str:
    """`x86_64`/`amd64`/`AMD64` → `x64`; `aarch64`/`arm64`/`ARM64` → `arm64`; else lower-cased as-is."""
    raw = (machine or "").strip().lower()
    return _ARCH_ALIASES.get(raw, raw)


@dataclass(frozen=True)
class Paths:
    """The node's filesystem layout on one OS. On Linux every value equals the packaged literal, except a
    community-edition node's `bundle_root`, `lib_root` and `python` (`linux._ce_paths`).

    etc_dir       the node's config directory (holds `node.env`)
    run_dir       the runtime tree every daemon-owned socket and request marker lives under
    creds_socket  the credential broker's fixed contract path
    state_dir     the DAEMON's own state root (never the session user's tree)
    bundle_root   where the installed bundle lives
    lib_root      the installed standard hooks and gates run from (the gate runner, …)
    python        the interpreter the daemon's scheduled tasks run under
    home_root     the session user's jaah home (`JAAH_HOME`'s packaged value)
    docker_host   the container engine endpoint a runner is pointed at; "" = the engine's own default
    tmux          the tmux socket directory (`TMUX_TMPDIR`)
    log_dir       where the service manager writes each Jaah service's stdout/stderr file (`jaahd.log`,
                  `runner.log`, …), which `node_logs` ships; "" = no such files, the journal holds them (Linux)
    """

    etc_dir: str
    run_dir: str
    creds_socket: str
    state_dir: str
    bundle_root: str
    lib_root: str
    python: str
    home_root: str
    docker_host: str
    tmux: str
    log_dir: str = ""

    @property
    def node_env(self) -> str:
        """The node's identity-and-credential file."""
        return f"{self.etc_dir}{self._sep}node.env"

    @property
    def _sep(self) -> str:
        return "\\" if "\\" in self.etc_dir else "/"


class Peer(NamedTuple):
    """The process on the far end of a local socket, as the KERNEL reports it.

    `principal` is the OS's account identity as a string: the numeric uid on posix, a SID on Windows.
    `pid`/`principal` of `-1`/`"-1"` mean the kernel could not be asked."""

    pid: int
    principal: str

    @property
    def uid(self) -> int:
        """The posix uid, or -1 when `principal` is not one."""
        try:
            return int(self.principal)
        except ValueError:
            return -1


UNKNOWN_PEER = Peer(-1, "-1")


def absent(*_args) -> None:
    """None for a seam question this OS does not answer: each implementation binds it under the names it
    leaves unread (`mount_namespace`, `stdin_tty`, `child_pids`, `virtualization`, `kvm`)."""
    return None

Runner = Callable[..., "subprocess.CompletedProcess"]


class Service:
    """One supervised daemon unit, driven through the OS's service manager.

    `restart_argv`/`active_argv` are the only per-OS facts; `run` is injectable so a test never touches
    the real supervisor."""

    def __init__(self, name: str, *, restart_argv: Sequence[str], active_argv: Sequence[str],
                 active_check: Callable[["subprocess.CompletedProcess"], bool] | None = None,
                 run: Runner | None = None, timeout: float = 30.0) -> None:
        self.name = name
        self._restart_argv = list(restart_argv)
        self._active_argv = list(active_argv)
        self._active_check = active_check or (lambda proc: proc.returncode == 0)
        self._run = run or subprocess.run
        self._timeout = timeout

    def restart(self) -> bool:
        """Restart the unit; True when the service manager accepted it. Never raises."""
        try:
            proc = self._run(self._restart_argv, capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def is_active(self) -> bool:
        """Whether the unit is running now. Never raises; an unaskable supervisor is "not active"."""
        try:
            proc = self._run(self._active_argv, capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        return bool(self._active_check(proc))


def metrics_sample(cpu_totals, mem, reboot_required, last_upgrade_epoch, net_totals=None) -> dict:
    """One instantaneous host sample — the shape every `sample_metrics()` returns, which
    `host_metrics.HostMetricsSampler` folds into the identical wire block on every OS.

      cpu_totals          `(total_ticks, busy_ticks)` cumulative counters, or None
      mem                 `(mem_pct, mem_total_kb)`, or None
      reboot_required     bool, or None when this OS has no such signal
      last_upgrade_epoch  int seconds, or None
      net_totals          `(rx_bytes, tx_bytes)` cumulative counters, or None where this OS has no reader
    """
    return {
        "cpu_totals": cpu_totals,
        "mem": mem,
        "reboot_required": reboot_required,
        "last_upgrade_epoch": last_upgrade_epoch,
        "net_totals": net_totals,
    }
