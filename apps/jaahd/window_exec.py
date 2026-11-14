"""The first program a runner window execs (`fleet_launch.isolated_window_argv`): rebuilds the pane's
environment from sources a same-uid `tmux set-environment` cannot write, then execs the window command.

tmux hands a new pane its server's GLOBAL environment overlaid by the session's, and any process of the
server's uid may rewrite both. The server's STARTUP environment (`/proc/<server>/environ`, the unit's
`Environment=`/`EnvironmentFile=` it was exec'd with) is immutable to that, and the pane is the server's
direct child. So the pane's environment is: that startup environment, the launcher's `K=V` pairs (what
`tmux new-window -e` used to override), and the values tmux sets per pane (`$TMUX_PANE` the hooks find their
window by, `TERM`). Nothing else. Run with `python -I` so no `PYTHON*` variable reaches this interpreter.

Stdlib only, no repo import: it runs as the session user, before any environment is trusted.

Usage: window_exec.py K=V ... -- SHELL CMD
"""
from __future__ import annotations

import os
import sys

# Set by tmux for each pane AFTER the global/session copy, so a planted one is overwritten.
PANE_PASSTHROUGH = (b"TMUX", b"TMUX_PANE", b"TERM", b"TERM_PROGRAM", b"TERM_PROGRAM_VERSION", b"PWD")


def server_startup_env(server_pid: int) -> dict[bytes, bytes]:
    """The tmux server's exec-time environment. Raises OSError when unreadable: a guessed environment is
    worse than a loudly failed window."""
    with open(f"/proc/{server_pid}/environ", "rb") as fh:
        raw = fh.read()
    env: dict[bytes, bytes] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key:
            env[key] = value
    return env


def window_env(startup: dict[bytes, bytes], pairs: list[str], ambient: dict[bytes, bytes]) -> dict[bytes, bytes]:
    """STARTUP, the launcher's PAIRS over it, then tmux's per-pane values from AMBIENT."""
    env = dict(startup)
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"window_exec: not a K=V pair: {pair!r}")
        env[os.fsencode(key)] = os.fsencode(value)
    for name in PANE_PASSTHROUGH:
        if name in ambient:
            env[name] = ambient[name]
    return env


def main(argv: list[str]) -> int:
    try:
        split = argv.index("--")
        shell, cmd = argv[split + 1], argv[split + 2]
    except (ValueError, IndexError):
        print("usage: window_exec.py K=V ... -- SHELL CMD", file=sys.stderr)
        return 2
    try:
        env = window_env(server_startup_env(os.getppid()), argv[:split], dict(os.environb))
    except (OSError, ValueError) as exc:
        print(f"window_exec: refusing to start the window with an unverified environment: {exc}",
              file=sys.stderr)
        return 1
    os.execve(shell, [shell, "-c", cmd], env)
    return 1  # unreachable: execve replaces this process or raises


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
