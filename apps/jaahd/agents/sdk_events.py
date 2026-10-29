"""sdk_events — the session reader for an SDK agent run (architecture §3.7/§4.7). NOT an `AgentAdapter`.

An SDK agent is not a provider (N1/N6): it is never registered in this package's adapter registry and never
answers `get_adapter`. What the session sweep needs from it is narrow, so this module is narrow too:

* `events_path(session_id)` — WHERE a run's events file is: `log_dir("runner")/agent-<session id>.events.jsonl`,
  node-local and outside the worktree, derivable from the session id alone. The launcher (`agent_process`)
  hands the same path to the run; this is its single home. A MicroVM run writes it into the VM's
  report share instead (`VM_EVENTS_NAME`), found through the VM registry by the run's session id while the VM
  lives, and at `kept_events_path` once `keep_vm_events` has copied it out ahead of the VM's destroy.
* `read_usage(line)` — one events line's token spend: `type=usage` lines in the shape Efficiency already
  reads (`$.message.id/model/usage.*`, DD8) become `TokenCounts`; every other line spends nothing.

The events file is the session's ONE transcript member (`main.jsonl`); redaction, gzip, upload and the
honest states are the sweep's own, unchanged.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import time
from pathlib import Path
from typing import Optional

import cloud_modules
import env_helpers

from .base import TokenCounts, count_int, json_object

log = logging.getLogger("jaahd.sdk_events")

#: `sessions.agent_kind` of an SDK agent run (`apps/api/app/agent_sessions.py`); the work row carries it.
AGENT_KIND = "sdk_agent"
#: The events file's name inside a MicroVM's report share (`microvm_session.agent_launch_files`).
VM_EVENTS_NAME = "agent-events.jsonl"


def events_path(session_id: str) -> Path:
    return env_helpers.log_dir("runner") / f"agent-{session_id}.events.jsonl"


#: Where a MicroVM run's events file is kept once its VM is gone: the daemon's own state, never a guest share.
KEPT_DIR = "sdk-events"
#: A copied events file is cut here: the guest writes it, so its size is the guest's to choose. Under the session
#: sweep's own source ceiling (`session_sweep.MAX_SOURCE_BYTES`), which refuses anything larger anyway.
KEEP_CAP_BYTES = 64 << 20
#: Nothing is kept when the daemon's state disk has less than this free: it also holds the node's keys.
KEEP_MIN_FREE_BYTES = 1 << 30
#: A kept copy older than this is pruned: the sweep uploads an ended session within its own retry bound.
KEEP_DAYS = 7
_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")  # as `session_sweep._SESSION_ID` (parity-tested)


def kept_events_path(session_id: str) -> Path:
    return env_helpers.daemon_state_dir(KEPT_DIR, f"agent-{session_id}.events.jsonl")


def candidates(session_id: str) -> list[tuple[Path, Path]]:
    """`(path, the root it must resolve inside)` for every place this run's events file may be: the static
    node's log dir first, then the report share of the MicroVM that ran it, if any, then the copy kept from a
    destroyed VM (`keep_vm_events`), and last the ACP event stream of a manifest 2 run (no supervisor writes
    this file for it; `acp/runtime.py` keeps its events under the daemon's `acp-runs`, static or MicroVM)."""
    found = [(events_path(session_id), env_helpers.log_dir("runner"))]
    if _SESSION_ID.fullmatch(session_id):
        found.append((kept_events_path(session_id), env_helpers.daemon_state_dir(KEPT_DIR)))
        from acp import runtime  # noqa: PLC0415 — deferred: only the sweep's lookup needs the hub's dir

        acp_runs = env_helpers.daemon_state_dir(runtime.RUNS_DIR)
        found.append((acp_runs / f"{session_id}.events.jsonl", acp_runs))
    try:
        microvm_host = cloud_modules.load("microvm_host")  # deferred: only a MicroVM node has rows to read

        rows = microvm_host.read_registry() if microvm_host is not None else {}
    except Exception:  # noqa: BLE001 — no registry is "not a MicroVM run", never a sweep failure
        rows = {}
    for vm_id, row in (rows or {}).items():
        if isinstance(row, dict) and str(row.get("runner_session_id") or "") == session_id:
            report = microvm_host.vmdir(str(vm_id)) / "report"
            found.append((report / VM_EVENTS_NAME, report))
    return found


def keep_vm_events(vm_id: str, vdir: Path) -> None:
    """A `microvm_rail` transcript collector: copy an SDK run's events file out of the VM's report share before
    the destroy deletes the VM dir, so the session sweep can still upload it once the CP ends the session (the CP
    ends it in the same sweep that orders the destroy). A VM with no events file (a built-in runner's) is skipped.
    The guest owns the share, so the source is opened without following links and must be a regular file
    inside it; the copy is capped at `KEEP_CAP_BYTES`."""
    import host_platform  # noqa: PLC0415 — deferred, as `candidates`
    microvm_host = cloud_modules.load("microvm_host")

    row = microvm_host.read_registry().get(vm_id) or {}
    session_id = str(row.get("runner_session_id") or "")
    if not _SESSION_ID.fullmatch(session_id):
        return
    report = vdir / "report"
    try:
        fd = os.open(report / VM_EVENTS_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or Path(host_platform.fd_path(fd)).parent != report.resolve():
            log.warning("sdk_events: %s of VM %s is not a regular file in its report share — not kept",
                        VM_EVENTS_NAME, vm_id)
            return
        target = kept_events_path(session_id)
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        _prune(target.parent)
        st = os.statvfs(target.parent)
        if st.f_bavail * st.f_frsize < KEEP_MIN_FREE_BYTES + KEEP_CAP_BYTES:
            log.warning("sdk_events: too little free disk to keep %s of VM %s", VM_EVENTS_NAME, vm_id)
            return
        tmp = target.with_name(f"{target.name}.tmp")
        try:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
            try:
                left = KEEP_CAP_BYTES
                while left > 0:
                    chunk = os.read(fd, min(1 << 20, left))
                    if not chunk:
                        break
                    os.write(out, chunk)
                    left -= len(chunk)
            finally:
                os.close(out)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    finally:
        os.close(fd)


def drop_kept(session_id: str) -> None:
    """Delete the copy `keep_vm_events` kept for `session_id`, once the sweep has uploaded it."""
    if _SESSION_ID.fullmatch(session_id):
        kept_events_path(session_id).unlink(missing_ok=True)


def _prune(kept: Path) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in kept.glob("agent-*.events.jsonl*"):
        try:
            if old.lstat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def _int(value: object) -> int:
    """`base.count_int`, clamped at zero: the control plane refuses a negative count."""
    return max(0, count_int(value))


def read_usage(line: bytes) -> Optional[TokenCounts]:
    """The spend one events line records, or None (not a usage line, or not readable as one)."""
    record = json_object(line)
    if record is None or record.get("type") != "usage":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    model = message.get("model")
    usage = message.get("usage")
    if not isinstance(model, str) or not model or not isinstance(usage, dict):
        return None
    return TokenCounts(
        model=model,
        input=_int(usage.get("input_tokens")),
        cache_write_5m=_int(usage.get("cache_creation_input_tokens")),
        cache_read=_int(usage.get("cache_read_input_tokens")),
        output=_int(usage.get("output_tokens")),
    )
