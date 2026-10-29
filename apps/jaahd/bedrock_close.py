"""K10 — `jaah.close_session`: end every pane this node launched on one Jaah session, and ack.

The control plane sends it (args `{"jaah_session_id": "<uuid>"}`) when a session must stop — a tenant at its
balance floor, a session it ended. BEST EFFORT by design: revoking the tenant on the AWS side is the
guarantee (the invoke role's `jaah-tenant-deny` policy); this only makes the stop prompt. The panes come from
`runner_registry` — the record written at launch, pid AND start time, so a recycled pid is never named —
and each is killed by its own pane id on the tmux server it lives on, never by a name another server could
resolve. The fleet holder window is never a target. The pane's death is what ends its live metering
(`bedrock_usage_tail` posts its last calls), so nothing else is touched here.
"""
from __future__ import annotations

import json
import logging
import subprocess
import uuid

import cred_broker
import env_helpers
import kill_gates
import runner_registry
import session_user
from commands import native_command
from fleet_tmux_server import HOLDER_WINDOW

log = logging.getLogger("jaahd.bedrock_close")

COMMAND_TYPE = "jaah.close_session"


def _pane_window(socket: str, pid: int, run) -> tuple[str, str] | None:
    """`(pane id, window name)` of the pane whose process is `pid` on one tmux server, or None. Filtered
    server-side to that one pane: the bus caps a shell-out's stdout, and a whole-server listing on a busy
    node could lose the very row asked for."""
    try:
        listed = run(
            session_user.tmux_cmd(socket, "list-panes", "-a", "-f", f"#{{==:#{{pane_pid}},{pid}}}",
                                  "-F", "#{pane_pid}\t#{pane_id}\t#{window_name}"),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("bedrock-close: looking up pane %s on socket %r failed (%s)", pid, socket, exc)
        return None
    if getattr(listed, "returncode", 1) != 0:
        return None
    for line in (getattr(listed, "stdout", "") or "").splitlines():
        found, _, rest = line.partition("\t")
        pane, _, name = rest.partition("\t")
        if found.strip() == str(pid) and pane.startswith("%"):
            return pane, name
    return None


def _bus_run(run):
    """The bus's injected `_native_run` (`commands.py`: `(*argv, timeout=) -> {"exit_code", "stdout"}`) in the
    `subprocess.run` shape this module speaks, so the handler's tmux calls go through the bus's bounded
    shell-out like every other native handler's."""
    def adapted(argv, *, capture_output=True, text=True, timeout=10, env=None):
        del capture_output, text  # `subprocess.run`'s shape only: the bus always captures text
        out = run(*argv, timeout=timeout, env=env)
        return subprocess.CompletedProcess(argv, out.get("exit_code", 1), out.get("stdout", ""), out.get("stderr", ""))
    return adapted


def close_session(jaah_session_id: str, *, run=None) -> dict:
    """Kill every live pane launched on `jaah_session_id`. Returns `{"jaah_session_id", "panes", "killed"}`."""
    sid = str(uuid.UUID(str(jaah_session_id)))
    run = run or subprocess.run
    panes = runner_registry.bedrock_panes(sid)
    killed: list[int] = []
    for socket in kill_gates._reachable_sockets():
        if len(killed) == len(panes):
            break
        for pid in panes:
            found = None if pid in killed else _pane_window(socket, pid, run)
            if found is None:
                continue
            pane, name = found
            if name == HOLDER_WINDOW:
                log.warning("bedrock-close: pane %s resolved to the fleet holder window — not killing it", pid)
                continue
            try:
                # The PANE, not its window: a human may have split a console tab, and their shell is no
                # part of the Jaah session.
                done = run(session_user.tmux_cmd(socket, "kill-pane", "-t", pane),
                           capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env())
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("bedrock-close: killing pane %s on socket %r failed (%s)", pane, socket, exc)
                continue
            if getattr(done, "returncode", 1) == 0:
                killed.append(pid)
                runner_registry.forget(pid)
    # A pane that exited on its own between the registry read and the kill is closed, not a failure.
    still_live = set(runner_registry.bedrock_panes(sid))
    killed.extend(pid for pid in panes if pid not in killed and pid not in still_live)
    # The broker serves this daemon's cache: a closed session's credentials are not handed out again.
    cred_broker.forget_bedrock_session(sid)
    log.info("bedrock-close: Jaah session %s — %d live pane(s), %d killed", sid, len(panes), len(killed))
    return {"jaah_session_id": sid, "panes": len(panes), "killed": len(killed)}


@native_command(COMMAND_TYPE)
def handle_close_session(command_type: str, args: dict, run) -> dict:
    """The bus handler: `{"jaah_session_id": "<uuid>"}` → the ack. A session with no live pane here is
    acked too (`killed: 0`) — already closed is the outcome asked for. A pane that could not be killed is
    `failed`, so the CP sees the stop did not land; revocation still bounds it."""
    try:
        result = close_session(str(args.get("jaah_session_id") or ""), run=_bus_run(run))
    except ValueError:
        return {"exit_code": 2, "stdout": "", "stderr": "jaah.close_session needs a jaah_session_id uuid"}
    ok = result["killed"] == result["panes"]
    return {
        "exit_code": 0 if ok else 1,
        "stdout": json.dumps(result),
        "stderr": "" if ok else f"{result['panes'] - result['killed']} pane(s) of the session could not be killed",
    }
