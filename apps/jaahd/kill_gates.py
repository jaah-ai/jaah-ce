"""The ONE choke point for an automated tmux-window kill — MECHANICS, not policy.

The node never decides a kill; it ENACTS one the control plane issued (no node-side arm gate may veto a
CP intent). What stays here is what makes a kill land on the RIGHT window: the window-id resolve across
both tmux sockets, the `HOLDER_WINDOW` refusal, and the fail-closed `_window_cwd_matches` identity check
against the slot's worktree path — never a persisted `session:index`, which renumbering retargets.

A MicroVM session (`vm:<vm_id>`) has no window: its kill is `kill_vm` — the VM stopped through
`jaah-microvmd`, then its transcripts settled — and no tmux command and no holder rule apply (the MicroVM
design's D20).

Every refused kill is traced in `force-kill-dryrun.log`.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import cloud_modules
import env_helpers
import host_platform
import session_user
from fleet_tmux_server import HOLDER_WINDOW  # the fleet session's keep-alive window — never a kill target

log = logging.getLogger("jaahd.kill_gates")

#: A kill target naming a MicroVM session rather than a tmux window: `vm:<vm_id>`.
VM_TARGET_PREFIX = "vm:"
#: The VM's ACPI grace before microvmd escalates (D20: shutdown, then SIGKILL after 10 s).
VM_STOP_GRACE_S = 10

def _record_project(record: dict) -> str:
    """The pool project name of a worktree record: ctl.py's injected `_project`, else `project`."""
    return record.get("_project") or record.get("project") or "unknown"


def _reachable_sockets() -> list[str]:
    """The tmux sockets a kill must try, in `session_user.manage_sockets()` order, minus any whose socket
    file is not there.

    A tmux client with no reachable server STARTS ONE as the caller's uid, which on the fleet socket
    would plant a `jaahd`-owned server where every dispatch connects. `""` (tmux's default socket, a
    no-session-user host) is always kept."""
    return [socket for socket in session_user.manage_sockets() if not socket or os.path.exists(socket)]


def _window_id_on(socket: str, tmux_target: str, run_fn) -> str:
    """The stable `#{window_id}` of `tmux_target` on this socket, or "" when absent or unprobeable. Also
    the per-socket membership test: window ids are per-server."""
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, "display-message", "-t", tmux_target, "-p", "#{window_id}"),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(
            "kill-gates: tmux window_id resolve for %s on socket %r failed (%s)",
            tmux_target, socket, exc,
        )
        return ""
    if getattr(probe, "returncode", 1) != 0:
        return ""
    return (getattr(probe, "stdout", "") or "").strip()


def _window_cwd_matches(socket: str, target: str, expect_cwd: str, run_fn) -> bool:
    """Whether ANY pane of TARGET on this socket has its cwd at — or under — EXPECT_CWD.

    The kill's IDENTITY check. FAILS CLOSED: an unreadable pane list is not a match and the kill is
    refused. A persisted `session:index` goes stale when `renumber-windows` shifts the pack, so the same
    string can name a different live window; unproven identity must never be killed on an index."""
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, "list-panes", "-t", target, "-F", "#{pane_current_path}"),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(
            "kill-gates: tmux pane-cwd probe for %s on socket %r failed (%s) — refusing the kill "
            "(an unverifiable window is not a kill target)", target, socket, exc,
        )
        return False
    if getattr(probe, "returncode", 1) != 0:
        return False
    # `host_platform.is_under`: the posix rule is the string prefix it always was; on Windows tmux answers
    # in MSYS spelling (`/c/ProgramData/...`) while the slot path is `C:\ProgramData\...`.
    for path in (getattr(probe, "stdout", "") or "").splitlines():
        if host_platform.is_under(path.strip(), expect_cwd):
            return True
    return False


def _window_holds_pane(socket: str, target: str, pane_pid: int, run_fn) -> bool:
    """Whether TARGET on this socket still holds the pane whose `#{pane_pid}` is PANE_PID. FAILS CLOSED, like
    `_window_cwd_matches`: a caller that named the pane wants exactly that pane's window and no other."""
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, "list-panes", "-t", target, "-F", "#{pane_pid}"),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if getattr(probe, "returncode", 1) != 0:
        return False
    return str(pane_pid) in {line.strip() for line in (getattr(probe, "stdout", "") or "").splitlines()}


def _window_name_on(socket: str, target: str, run_fn) -> str:
    """`#{window_name}` of TARGET on this socket, or "". Feeds only the holder refusal, so an unreadable
    name fails OPEN (not-the-holder): the kill proceeds and no-ops on a gone window."""
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, "display-message", "-t", target, "-p", "#{window_name}"),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(probe, "returncode", 1) != 0:
        return ""
    return (getattr(probe, "stdout", "") or "").strip()


def kill_session(tmux_target: str, *, expect_cwd: str = "", reason: str = "", socket: str | None = None,
                 expect_pane_pid: int | None = None, run_fn=subprocess.run) -> bool:
    """Best-effort kill of a session's tmux window by its stable `#{window_id}` (resolved immediately
    before killing — a `session:index` is a TOCTOU under renumbering), across EVERY socket in
    `session_user.manage_sockets()`: an in-place-upgraded node keeps pre-switch runners on the daemon's
    own server, and a False leaves `reconcile.enact_signal._enact_kill_runner`'s intent undrained.

    The raw-target fallback is confined to a one-socket set: with two servers `<node>:3` names a
    different real window on each. `expect_cwd` (the slot record's `path`) makes the resolve an identity
    check, fail-closed (`_window_cwd_matches`). `reason` rides the confirmed-kill log line.

    Returns True on the first clean kill, False otherwise (gone, no tmux, non-zero exit, OS error);
    logged, never raised. A `vm:<vm_id>` target is `kill_vm`'s, and never reaches tmux.

    `socket`: the one server the caller resolved `tmux_target` on (window ids are per-server, so `@7` names a
    different window on each); only that socket is tried, with no raw-target fallback. `expect_pane_pid`: the
    window must still hold that pane (`_window_holds_pane`, fail-closed)."""
    if not tmux_target:
        return False
    if tmux_target.startswith(VM_TARGET_PREFIX):
        return _kill_registered_vm(tmux_target[len(VM_TARGET_PREFIX):], reason)
    sockets = _reachable_sockets()
    pinned = socket is not None
    if socket is not None:
        sockets = [s for s in sockets if s == socket]
    if not sockets:
        log.warning(
            "kill-gates: no reachable tmux socket for %s (fleet=%r) — nothing killed",
            tmux_target, session_user.fleet_socket(),
        )
        return False
    for socket in sockets:
        target = _window_id_on(socket, tmux_target, run_fn)
        if not target:
            if len(sockets) > 1 or pinned:
                continue   # not this server's window — see the docstring on the raw-target fallback
            target = tmux_target
        name = _window_name_on(socket, target, run_fn)
        if name == HOLDER_WINDOW:
            # NEVER the holder: it keeps this node's session (every console and dispatch) alive and no slot
            # owns it. A stale raw-target fallback can resolve to it; refuse and report "did not kill".
            log.warning(
                "kill-gates: refusing to kill %s on socket %r — it resolved to the fleet holder "
                "window (%s), which no slot owns; the caller's tmux_target %r is stale",
                target, socket, HOLDER_WINDOW, tmux_target,
            )
            continue
        if expect_cwd and not _window_cwd_matches(socket, target, expect_cwd, run_fn):
            log.warning(
                "kill-gates: refusing to kill %s (%r) on socket %r — no pane of it lives under %s, so "
                "the caller's tmux_target %r names a DIFFERENT window than the slot it wants to kill "
                "(a stale session:index renumbered onto a live neighbour)%s",
                target, name, socket, expect_cwd, tmux_target, f" — {reason}" if reason else "",
            )
            continue
        if expect_pane_pid is not None and not _window_holds_pane(socket, target, expect_pane_pid, run_fn):
            log.warning(
                "kill-gates: refusing to kill %s (%r) on socket %r — it no longer holds pane pid %s%s",
                target, name, socket, expect_pane_pid, f" — {reason}" if reason else "",
            )
            continue
        try:
            proc = run_fn(
                session_user.tmux_cmd(socket, "kill-window", "-t", target),
                capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("kill-gates: tmux kill-window %s on socket %r failed: %s", target, socket, exc)
            continue
        if getattr(proc, "returncode", 1) == 0:
            # A confirmed kill is logged so the journal names why every window died.
            log.info(
                "kill-gates: killed tmux window %s (%r) on socket %r for target %r%s",
                target, name, socket, tmux_target, f" — {reason}" if reason else "",
            )
            return True
        log.warning(
            "kill-gates: tmux kill-window %s on socket %r exited %s: %s",
            target, socket, getattr(proc, "returncode", "?"), getattr(proc, "stderr", ""),
        )
    return False


def kill_vm(vm_id: str, *, reason: str = "", vmdir: Path | None = None, request=None, drain=None) -> bool:
    """Stop a MicroVM session's VM, then settle its transcripts — D20's kill, steps 1-3. True only when both
    happened; the caller removes `<vmdir>` ONLY THEN, because `<vmdir>/config` holds the only copy of the
    session's transcripts and the removal is not undoable.

    No tmux and no holder rule: a VM is one session, so stopping it ends exactly that session. The stop goes
    FIRST so the transcripts are final when drained (as the environment rail's `env.stop` → drain did); a VM
    the supervisor no longer holds is already stopped. Logged, never raised."""
    microvm_client = cloud_modules.load("microvm_client")  # Linux-only machinery, off the macOS/Windows import graph
    microvm_console = cloud_modules.load("microvm_console")
    microvm_registry = cloud_modules.load("microvm_registry")
    microvmd = cloud_modules.load("microvmd")

    if microvmd is None or not microvmd.valid_vm_id(vm_id):
        log.warning("kill-gates: refusing to kill VM %r — not a VM id", vm_id)
        return False
    request = request or microvm_client.request
    try:
        request("stop", vm_id=vm_id, grace_s=VM_STOP_GRACE_S, timeout=VM_STOP_GRACE_S + 60.0)
    except (microvm_client.SupervisorError, OSError) as exc:
        held = {row.get("vm_id") for row in _vm_rows(request)}
        if vm_id in held or held == {None}:
            log.warning("kill-gates: VM %s did not stop (%s)%s", vm_id, exc, f" — {reason}" if reason else "")
            return False
        log.info("kill-gates: VM %s is not held by the supervisor — already stopped", vm_id)
    binding = microvm_registry.load().get(vm_id)
    try:
        (drain or microvm_console.drain_vm_transcripts)(
            vm_id, vmdir or microvmd.vms_dir() / vm_id,
            runner_session_id=binding.runner_session_id if binding is not None else "",
        )
    except microvm_console.TranscriptDrainBlocked as exc:
        log.warning("kill-gates: VM %s stopped, but its transcripts are not settled (%s) — its dir must "
                    "be kept", vm_id, exc)
        return False
    log.info("kill-gates: stopped VM %s and settled its transcripts%s", vm_id, f" — {reason}" if reason else "")
    return True


def _kill_registered_vm(vm_id: str, reason: str) -> bool:
    """`kill_session`'s VM route: only a VM this node's registry binds to a session — the VM spelling of the
    window's cwd proof. (A CP `destroy`, named by its VM id, calls `kill_vm` itself.)"""
    if host_platform.is_windows():
        return _kill_windows_vm(vm_id, reason)
    microvm_registry = cloud_modules.load("microvm_registry")

    if microvm_registry is None:
        log.warning("kill-gates: refusing to kill VM %r — this node runs no MicroVM", vm_id)
        return False
    if vm_id not in microvm_registry.load():
        log.warning("kill-gates: refusing to kill VM %r — no registry row binds it to a session", vm_id)
        return False
    return kill_vm(vm_id, reason=reason)


def _kill_windows_vm(vm_id: str, reason: str) -> bool:
    """`_kill_registered_vm` on a marked Windows node: the WSL2 backend holds the registry and the supervisor, and
    runs this same route (`microvm_bridge` `vm_intent`, its `no_vm` the registry refusal)."""
    bridge = cloud_modules.load("microvm_bridge")
    client = bridge.windows_client() if bridge is not None else None
    if client is None or not bridge.windows_marked():
        log.warning("kill-gates: refusing to kill VM %r — this node runs no MicroVM", vm_id)
        return False
    reply = client.vm_intent({"kind": "kill", "vm_id": vm_id, "reason": reason}, timeout=bridge.KILL_TIMEOUT_S)
    if reply is None or reply.get("op") == "error":
        log.warning("kill-gates: VM %s was not killed: %s", vm_id,
                    "the MicroVM backend did not answer" if reply is None else reply.get("message"))
        return False
    return reply.get("ok") is True


def _vm_rows(request) -> list[dict]:
    """The supervisor's VM rows; `[{}]` (a row whose id is None) when it cannot be asked, so a caller never
    reads "unreachable" as "not held"."""
    microvm_client = cloud_modules.load("microvm_client")

    try:
        return [row for row in request("list", timeout=5.0).get("vms") or [] if isinstance(row, dict)]
    except (microvm_client.SupervisorError, OSError):
        return [{}]


def _dryrun_log_path() -> Path:
    """`<log_dir>/hook/force-kill-dryrun.log`."""
    return env_helpers.log_dir("hook", "force-kill-dryrun.log")


def _append_dryrun_log(message: str) -> None:
    """Append a timestamped dry-run line. Best-effort: a logging failure never breaks the kill pass."""
    stamp = env_helpers.now_iso()
    path = _dryrun_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError as exc:
        log.warning("kill-gates: could not append to force-kill-dryrun.log (%s): %s", path, exc)


def guarded_window_kill(
    tmux_target: str, reason: str, *, expect_cwd: str = "", socket: str | None = None,
    expect_pane_pid: int | None = None, run_fn=subprocess.run
) -> bool:
    """The ONE Python choke point for an AUTOMATED tmux-window kill: enact the CP's kill through
    `kill_session` (the window_id-stable, cwd-verified kill) and return whether a window actually died.

    No arm gate: the CP decided. A False (gone, unresolvable, refused on identity) is recorded in the
    dry-run log."""
    killed = kill_session(tmux_target, expect_cwd=expect_cwd, reason=reason, socket=socket,
                          expect_pane_pid=expect_pane_pid, run_fn=run_fn)
    if not killed:
        _append_dryrun_log(
            f"fleet: NOTHING KILLED for tmux window '{tmux_target}' — {reason}; the window was gone, "
            f"unresolvable, or refused on identity (see the kill-gates warnings above it)"
        )
    return killed
