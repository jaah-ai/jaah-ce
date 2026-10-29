#!/usr/bin/env python3
"""open_term.py — one-shot desktop-terminal pops for the console's host actions (Logs, Open in Cursor),
plus attach-by-target to an already-live tmux session. Spawning is `ctl.py pop-terminal`
(`fleet_launch.pop_terminal`). Best-effort: no DISPLAY is a clean no-op (exit 0). Desktop terminals,
not tmux tabs.

    python3 apps/jaahd/open_term.py "<title>" "<cwd>" "<command>"          # pop ONE terminal
    python3 apps/jaahd/open_term.py attach "<title>" "<session>:<window>"  # focus the live window
        # + pop ONE terminal attaching to <session>; creates NO new tmux session/tab
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import kill_gates  # `_reachable_sockets`: absent socket files skipped, so no client starts a server
import session_user

from env_helpers import fleet_ctl_path


def attach_command(session: str) -> str:
    """The shell command a human runs to attach to SESSION; single home of the `watch:` hint.

    Runs as a human's uid: a bare `tmux attach` would resolve that uid's own default socket and start an
    empty server. `jaah tmux` runs tmux as the socket's owner (sudo-as-owner), needing no group-widened
    socket; the explicit `-S` form is the fallback when the CLI is not on PATH and needs the socket
    group-usable (`session_user.share_own_socket`). No session user ⇒ plain `tmux attach`."""
    from fleet_launch import shq

    if session_user.session_uid() is None:
        return f"tmux attach -t {shq(session)}"
    if shutil.which("jaah"):
        return f"jaah tmux {shq(session)}"
    return f"tmux -S {shq(session_user.fleet_socket())} attach -t {shq(session)}"


def _pop_terminal(title: str, cmd: str) -> None:
    subprocess.run(
        [sys.executable, fleet_ctl_path(), "pop-terminal", title, cmd], env=os.environ.copy(),
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # FLEET_TERMINAL/FLEET_DESKTOP knobs (`env_helpers.load_fleet_env`); the environment wins.
    from env_helpers import load_fleet_env

    load_fleet_env()

    if argv and argv[0] == "attach":
        # Attach-by-target: focus the live window, then pop ONE terminal; creates no tmux session/tab.
        title = argv[1] if len(argv) > 1 else ""
        target = argv[2] if len(argv) > 2 else ""
        if not target:
            print("open_term.py attach: need a <session>:<window> target", file=sys.stderr)
            return 2
        if not shutil.which("tmux"):
            print("open_term.py attach: tmux required", file=sys.stderr)
            return 1
        from fleet_launch import tmux_safe_env

        # Sanitized env: a tmux client command auto-starts the server, which freezes the first caller's
        # env into every window it ever hosts (guard: env_helpers._tmux_safe_env).
        env = tmux_safe_env()
        session = target.split(":", 1)[0]
        # Walk the socket set: mid upgrade-drain a session may still be on the daemon's own server.
        sockets = kill_gates._reachable_sockets()
        # Best-effort focus; stop at the first server that owns it, so a same-named window elsewhere is not.
        for socket in sockets:
            focused = subprocess.run(
                session_user.tmux_cmd(socket, "select-window", "-t", target),
                env=env, capture_output=True,
            )
            if focused.returncode == 0:
                break
        # Pop only if no client is attached on ANY server (`=name` avoids tmux's prefix-matching);
        # otherwise every Console click would pop another terminal.
        for socket in sockets:
            clients = subprocess.run(
                session_user.tmux_cmd(socket, "list-clients", "-t", f"={session}"),
                env=env, capture_output=True, text=True,
            ).stdout
            if clients.strip():
                return 0
        _pop_terminal(title or session, attach_command(session))
        return 0

    # One-shot form: open_term.py <title> <cwd> <command>
    title = argv[0] if len(argv) > 0 else ""
    cwd = argv[1] if len(argv) > 1 else ""
    run = argv[2] if len(argv) > 2 else ""
    if not run:
        print(
            "usage: open_term.py <title> <cwd> <command>  |  "
            "open_term.py attach <title> <session>:<window>",
            file=sys.stderr,
        )
        return 2
    from fleet_launch import shq

    _pop_terminal(title, f"cd {shq(cwd)} && {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
