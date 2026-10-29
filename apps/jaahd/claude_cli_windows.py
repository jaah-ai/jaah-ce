"""claude_cli_windows.py — the Windows twin of the Linux package install's runner-CLI floor: the `jaah` account
must HAVE a `claude.exe`, and the pinned Claude ACP adapter the agent host drives (`acp/host.py`).

Anthropic's native Windows installer puts `claude.exe` under the INSTALLING user's `%USERPROFILE%\\.local\\bin`,
so it must run AS the runner account with that profile pinned to the runner home
(`host_platform.paths().home_root`, the same home launch.sh puts on PATH — `fleet_launch._windows_path_lines`).
The `jaah-runner` service runs `ensure` at start, before it keeps the fleet tmux server: a missing CLI is then
installed once per node, and `runner_clis` attests it on the next observation pass.

The adapter (`ACP_ADAPTER`) is converged after it, as macOS's `jaah-converge-clis` does:
`runner_cli_pins.install` into the runner home's `.local` prefix (its `bin\\claude-agent-acp.cmd` shim lands in
the same `.local\\bin`), at its pin and lock from the bundle, with the `npm.cmd` in the Node.js dir the installer
fills (`windows.NODEJS_DIR`). A current adapter is left alone, so a converged node does nothing. The two steps
are independent: a failed one never skips the other, and each failure is reported.

Run as: `<bundle python> <lib_root>\\apps\\jaahd\\claude_cli_windows.py ensure` — exit 0 when `claude.exe` and the
adapter are there (already, or after the install), 1 with the reasons on stderr otherwise, 2 on bad usage.
Stdlib only; every native step is a module-level name a test replaces.
"""
from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import runner_cli_pins
from host_platform import windows

# Anthropic's documented PowerShell one-liner for the native Windows install (docs: "Set up Claude Code").
INSTALL_SCRIPT = "irm https://claude.ai/install.ps1 | iex"
# The download + self-install of a ~200 MB binary on a slow link; past this the service start gives up and
# the next start retries.
INSTALL_TIMEOUT_S = 900
# `ensure`'s worst case: the install above, then the adapter's (`runner_cli_pins.install`). The runner service's
# cap on the whole step (`JaahService.cs`'s `EnsureMinutes`) must cover it.
ENSURE_BUDGET_S = INSTALL_TIMEOUT_S + runner_cli_pins.PINNED_INSTALL_BUDGET_S

_run = subprocess.run
_isfile = os.path.isfile
_is_runner_account = windows.is_runner_account
_which = shutil.which
_pins_stale = runner_cli_pins.stale
_pins_install = runner_cli_pins.install

# The ACP adapter a Claude runner's launch.sh runs under `jaah-acp-host` (`[[npm]]` in packaging/platform-pins.toml).
ACP_ADAPTER = "@agentclientprotocol/claude-agent-acp"


def cli_dir() -> str:
    """`<runner home>\\.local\\bin` — where the installer puts `claude.exe` for the runner account."""
    return ntpath.join(windows.paths().home_root, ".local", "bin")


def cli_path() -> str:
    return ntpath.join(cli_dir(), "claude.exe")


def adapter_prefix() -> str:
    """`<runner home>\\.local` — the npm prefix the adapter installs under (`bin\\` beside `claude.exe`)."""
    return ntpath.join(windows.paths().home_root, ".local")


def ensure() -> str:
    """"" when `claude.exe` and the adapter are in place (each converged first when not); the reasons
    otherwise, one per line."""
    return "\n".join(err for err in (ensure_claude(), ensure_adapter()) if err)


def ensure_adapter() -> str:
    """"" when `ACP_ADAPTER` is installed at its pin under `adapter_prefix()` (installing it first when not);
    the reason otherwise — npm's exit status and stderr tail among it (`runner_cli_pins.InstallError`)."""
    prefix = Path(adapter_prefix())
    try:
        # `stale` reads the installed version and lock stamp only: a shim lost after them (a failed link, a
        # deletion) would read as current forever and the node never sense the adapter.
        if not _pins_stale(prefix, [ACP_ADAPTER]) and _adapter_bin_present(prefix):
            return ""
    except runner_cli_pins.PinsError as exc:
        return f"claude_cli_windows: the {ACP_ADAPTER} pin cannot be read ({exc})"
    if not _is_runner_account():
        return (f"claude_cli_windows: {ACP_ADAPTER} is not at its pin under {prefix}, and this process is not "
                f"the {windows.RUNNER_ACCOUNT!r} account — run `ensure` as {windows.RUNNER_ACCOUNT!r}")
    # npm is `npm.cmd` in the Node.js dir the installer fills; CreateProcess would never find it as `npm`, so
    # it goes in by its full path (PATHEXT resolves it), and finds its own `node.exe` beside itself.
    npm = _which("npm", path=windows.NODEJS_DIR)
    if not npm:
        return (f"claude_cli_windows: no npm in {windows.NODEJS_DIR}, so {ACP_ADAPTER} cannot be installed — "
                f"Node.js is missing (the node installer puts it there)")
    try:
        _pins_install(prefix, [ACP_ADAPTER], npm=npm)
    except (runner_cli_pins.PinsError, runner_cli_pins.InstallError, runner_cli_pins.PrefixBusy, OSError,
            tarfile.TarError, ValueError, subprocess.SubprocessError) as exc:
        return f"claude_cli_windows: the {ACP_ADAPTER} install failed: {exc}"
    if _pins_stale(prefix, [ACP_ADAPTER]) or not _adapter_bin_present(prefix):
        return f"claude_cli_windows: the {ACP_ADAPTER} install finished but {prefix} is still not at its pin"
    return ""


def _adapter_bin_present(prefix: Path) -> bool:
    """Whether the adapter's `.cmd` shim (`runner_cli_pins`' Windows bin) is in `prefix\\bin`."""
    pin = runner_cli_pins.load_npm()[ACP_ADAPTER]
    return os.path.isfile(os.path.join(prefix, "bin", f"{pin.bin}.cmd"))


def ensure_claude() -> str:
    """"" when `claude.exe` is present (installing it first when absent); the reason otherwise.

    Refuses to install as any account but `jaah`: run as the daemon's service account it would leave a
    CLI the runner may not update itself (the native CLI self-updates in place)."""
    target = cli_path()
    if _isfile(target):
        return ""
    if not _is_runner_account():
        return (f"claude_cli_windows: {target} is missing, and this process is not the "
                f"{windows.RUNNER_ACCOUNT!r} account — run `ensure` as {windows.RUNNER_ACCOUNT!r}")
    home = windows.paths().home_root
    # The installer resolves its destination from the profile: pin it to the runner home, whatever
    # profile the service logon loaded.
    env = {**os.environ, "USERPROFILE": home, "HOME": home}
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", INSTALL_SCRIPT]
    try:
        proc = _run(argv, env=env, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"claude_cli_windows: the Claude Code installer could not run ({exc})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "(no output)"
        return f"claude_cli_windows: the Claude Code installer failed (rc={proc.returncode}): {detail}"
    if not _isfile(target):
        return (f"claude_cli_windows: the Claude Code installer exited 0 but left no {target} — "
                f"runners on this node cannot start")
    return ""


def main(argv: list[str]) -> int:
    if argv != ["ensure"]:
        print("usage: claude_cli_windows.py ensure", file=sys.stderr)
        return 2
    err = ensure()
    if err:
        print(err, file=sys.stderr)
        return 1
    print(f"claude_cli_windows: {cli_path()} present, {ACP_ADAPTER} at its pin under {adapter_prefix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
