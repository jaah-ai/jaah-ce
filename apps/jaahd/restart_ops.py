"""The operator's "restart this node now" command — `node_restart` (a native handler, as `upgrade_ops.py`).

A hard restart of THIS daemon: it answers, then exits with a non-zero code a moment later, so its service manager
starts it afresh (systemd `Restart=on-failure`, launchd `KeepAlive SuccessfulExit=false`, the Windows SCM's
recovery actions all restart a non-zero exit, and none restarts exit 0 — a revoked token's final exit). The units
run `KillMode=process` (and its launchd/SCM twins), so the runners and their tmux sessions keep running and the new
daemon re-adopts them. No args: nothing from the CP reaches the exit beyond the ask itself.
"""
from __future__ import annotations

import logging
import os
import threading

from commands import native_command

log = logging.getLogger("jaahd.restart_ops")

#: `EX_TEMPFAIL`: non-zero, so every service manager restarts the daemon; never 0 (that exit is final).
RESTART_EXIT_CODE = 75
#: Long enough for the reply frame to leave the socket before the process goes.
REPLY_GRACE_SECONDS = 2.0


def _exit_now() -> None:
    log.warning("node_restart: exiting %d for the service manager to restart this daemon", RESTART_EXIT_CODE)
    os._exit(RESTART_EXIT_CODE)


@native_command("node_restart")
def node_restart(command_type: str, args: dict, run) -> dict:
    """Schedule this daemon's hard exit and answer before it lands."""
    timer = threading.Timer(REPLY_GRACE_SECONDS, _exit_now)
    timer.daemon = True
    timer.start()
    return {"exit_code": 0, "stdout": "restarting: the node reconnects in a few seconds", "stderr": ""}
