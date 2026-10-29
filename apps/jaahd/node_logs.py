"""Ship this node's own systemd journal to the control plane, which writes it to CloudWatch Logs (SOC2
G-18: node journals leave the node, so a wiped or lost host still has an audit trail).

`journalctl -o json` per `UNITS`, oldest first after the persisted `__CURSOR`, goes to
`POST /api/agent/node-logs` as `{"cursor", "entries": [{ts, unit, priority, message}]}` in batches under
the CP's 1 MiB body cap. Every message passes `transcript_redact.redact_text` BEFORE it leaves the process:
a daemon log can echo a token, and CloudWatch is a store we never scrub.

The cursor advances only on the CP's acknowledgement, written atomically, so a failed POST re-sends at
most that one batch next pass — duplicates bounded to one batch, never a gap. A 503 (no log group
configured) or 403 (not a node) backs off `BACKOFF_SECONDS` with the cursor kept, so nothing is lost while
the CP is unconfigured. The one wider re-send: a cursor journalctl rejects (or a lost cursor file) restarts
from the 24 h backfill. A body the CP refuses (413/422) is split, so one bad entry never wedges the rest.

A second source, the kernel's own lines (`_TRANSPORT=kernel`), ships only its OOM lines (`KERNEL_MARKS`) as
`unit: "kernel"` under its own cursor file (`node-logs-kernel.cursor`): the CP reads an OOM kill off them
(`apps/api/app/node_log_findings.py`), and the rest of the kernel ring is noise. An OOM line about a virtual
node's process (`_is_vnode_kernel_line`) stays on the node too: it may be another tenant's, and a vnode's own
daemon ships no journal, so it would reach no one else either.

A runner unit runs untrusted workflow code, which can write its own lines into the unit's journal
(`logger`, `systemd-cat`) — a forged "Job … completed" or OOM sentence. From a runner unit only the runner's
own stdout ships (`_runner_line_admitted`); systemd's lines about the unit (`_SYSTEMD_UNIT=init.scope`) still do.

A line refused by either rule is skipped by storing its cursor locally — nothing is shipped for it, so
nothing is lost.

The console reads the shipped journal back in Node › Diagnostics (operator only), `routes/nodes/journal.py`;
the CP keys the stream by node id.

macOS and Windows have no journal: launchd / `JaahService.cs` write each service's stdout and stderr to
`<role>.log` under `host_platform.paths().log_dir`. There the same pass tails `LOG_FILES` instead, each by
byte offset under its own cursor file (`node-logs-file-<stem>.cursor`; the wire cursor is `<offset>:<file id>`,
advanced on the same acknowledgement), and posts the same entry shape: the unit is the Linux unit the file stands
in for, so the CP's folds (`recent_errors`, `update_history`) read it unchanged; the priority is the Python
logging level the line starts with (`main.py`'s format), else info; the timestamp the line's own, else now. The
pass's `MAX_BATCHES_PER_PASS` is shared round-robin, so a busy `jaahd.log` never starves the others. A first read
starts `FIRST_RUN_TAIL_BYTES` from the end, at a line start, not at the file's whole history; a file shorter
than its offset, or another file than the cursor's (its id: the inode, or Windows' file index), was truncated or
replaced and is read from 0 (a file truncated in place that regrew past the old offset before the next pass is
indistinguishable from an append — its head is skipped). A `jaahd.log` record its reporter posts itself
(`SELF_POSTED`: that logger's line carrying its mark) is skipped: it reaches the CP once, under the reporter's own
unit. The engine's and nodelink's logs are not shipped, as their
Linux units are not.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import re
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Mapping, Optional

import cloud_modules
import control_plane_api as control_plane_api_mod
import env_helpers
import host_platform
from jaah_env import env_get
import node_paths
import transcript_redact
import windows_anchor_health
import windows_engine_health
from vnodehelper import layout as vnode_layout

log = logging.getLogger("jaahd.node_logs")

# The GHA runner pool's unit naming; None on a community-edition node, which runs no pool.
gha_runner_ops = cloud_modules.load("gha_runner_ops")

# The PRD verify bar is "appears within 5 min".
INTERVAL_SECONDS = 30.0
BACKOFF_SECONDS = 600.0
MAX_ENTRIES_PER_READ = 2000
MAX_BATCHES_PER_PASS = 20
# Headroom under the CP's 1 MiB (1048576) cap.
MAX_BODY_BYTES = 900_000
# Bounds one entry far under MAX_BODY_BYTES even at json's worst-case 12 bytes per escaped char.
MAX_MESSAGE_CHARS = 16384
TRUNCATED_MARKER = "…[truncated]"
DEFAULT_PRIORITY = 6
# First run (no cursor): backfill a day rather than the journal's whole retention.
FIRST_RUN_SINCE = "-24h"
_JOURNALCTL_TIMEOUT_SECONDS = 60.0
# One `--all` entry can be ~32 MiB over journald's native protocol; a line past this is skipped unread, and
# a read stops after `MAX_READ_BYTES` (it resumes from the cursor next pass).
MAX_LINE_BYTES = 1024 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_UNIT_CHARS = 256  # the CP's bound

# The HOST node's own units. Deliberately NOT `jaahd@*.service` / `*-vn@*`: those are other virtual
# nodes on this machine, possibly a tenant's, whose logs are theirs to ship over their own token.
_HOST_UNITS = (
    ("daemon", None),
    ("update", None),
    ("vnodehelper", None),
    ("vnode_rebuild", None),
    ("gha", None),
    ("shared_accounts", None),
    ("runner", "*"),
)
UNITS = tuple(node_paths.unit_name(kind, instance) for kind, instance in _HOST_UNITS)

# The kernel source: only these lines leave the node (`oom-kill:` names the killed task's cgroup; "out of
# memory:" is the kill sentence, global or a cgroup's). Matched case-insensitively.
KERNEL_MARKS = ("oom-kill:", "out of memory:")
KERNEL_UNIT = "kernel"

# Off Linux: the service log files under `host_platform.paths().log_dir` → the unit each stands in for. The
# runner's is the template's own name (`jaah-runner@.service`): one runner service per host, no instance.
LOG_FILES = (
    ("jaahd.log", node_paths.unit_name("daemon")),
    ("runner.log", node_paths.unit_name("runner", "")),
    ("update.log", node_paths.unit_name("update")),
)
FIRST_RUN_TAIL_BYTES = 64 * 1024
# A line's own stamp and Python logging level, when it starts with them: `main.py`'s
# `2026-01-02T03:04:05Z WARNING jaahd.x: …`, or `JaahService.cs`'s local-time `2026-01-02T03:04:05 jaah-service[…]: …`.
_FILE_LINE_RE = re.compile(
    r"^(?:(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?)(Z?)\s+)?(?:(CRITICAL|ERROR|WARNING|INFO|DEBUG)[\s:])?")
_LEVEL_PRIORITY = {"CRITICAL": 2, "ERROR": 3, "WARNING": 4, "INFO": 6, "DEBUG": 7}
# Warnings a Windows reporter posts itself and also logs to `jaahd.log`, both through
# `windows_engine_health.post_warning`: the record `main.py`'s format writes for them, `<its logger>: <mark>…`.
SELF_POSTED = tuple(f" {windows_engine_health.log.name}: {mark}"
                    for mark in (windows_engine_health.MARK, windows_anchor_health.MARK))


def host_gha_unit_glob() -> Optional[str]:
    """This host's own GHA runner units: `actions.runner.<scope>.<anchor>-gha-<n>.service`, the anchor being
    the HOST's hostname (`gha_runner_ops.runner_prefix`). Anchored on it, not `actions.runner.*`: a virtual
    node's runners share the host's unit namespace but are named after the vnode (`vnodehelper/gha.py`
    refuses a vnode named like the host), and like `jaahd@*` they are another node's — possibly a tenant's —
    to ship over its own token. None when the host has no name to anchor on. Recomputed per pass, as
    `runner_prefix` is (a renamed host renames its runners)."""
    anchor = gha_runner_ops.runner_prefix() if gha_runner_ops is not None else ""
    return f"actions.runner.*.{anchor}-gha-*.service" if anchor else None


def _is_kernel_oom(entry: dict) -> bool:
    text = entry["message"].lower()
    return any(mark in text for mark in KERNEL_MARKS)


# Every uid a virtual node's processes run as: its principals' block, and the subordinate block its rootless
# containers map into (`vnodehelper/layout.py`).
_VNODE_UIDS = (
    range(vnode_layout.UID_BASE, vnode_layout.UID_BASE + vnode_layout.UID_STRIDE * (vnode_layout.SLOT_MAX + 1)),
    range(vnode_layout.SUBID_BASE,
          vnode_layout.SUBID_BASE + vnode_layout.SUBID_STRIDE * (vnode_layout.SLOT_MAX + 1)),
)
# `oom-kill:…,task_memcg=<path>,…,uid=<n>` and "Out of memory: Killed process … UID:<n> …".
_KERNEL_UID_RE = re.compile(r"(?:\buid=|\bUID:)(\d{1,10})")
_KERNEL_MEMCG_RE = re.compile(r"task_memcg=([^,\s]{1,4096})")
_CGROUP_USER_RE = re.compile(r"user[@-](\d{1,10})\.(?:service|slice)")


def _is_vnode_uid(uid: int) -> bool:
    return any(uid in span for span in _VNODE_UIDS)


def _is_vnode_kernel_line(message: str) -> bool:
    """Whether a kernel OOM line is about a virtual node's process: its uid is a vnode's
    (`_VNODE_UIDS`), or its `task_memcg` runs through a vnode unit (`jaahd@<t>`, `*-vn@<t>`) or a vnode uid's
    `user@`/`user-` cgroup. A vnode's runner units are named like any runner, so their uid decides."""
    if any(_is_vnode_uid(int(uid)) for uid in _KERNEL_UID_RE.findall(message)):
        return True
    memcg = _KERNEL_MEMCG_RE.search(message)
    for part in memcg.group(1).split("/") if memcg else ():
        if part.startswith("jaahd@") or "-vn@" in part:
            return True
        user = _CGROUP_USER_RE.fullmatch(part)
        if user and _is_vnode_uid(int(user.group(1))):
            return True
    return False


def _kernel_admitted(record: dict) -> bool:
    return not _is_vnode_kernel_line(_message_text(record.get("MESSAGE")))


# What a runner unit's own lines come from. journald stamps a stdout line with the process that OPENED the
# stream — the unit's main process (`runsvc.sh`, persistent; `run.sh`, ephemeral, exec'd by `gha_job.py run`),
# whose children (`Runner.Listener`) inherit it — never the child that wrote it. `Runner.Listener` (15
# characters: journald's 15-character `comm` keeps it whole) covers a runner that is its own main process.
RUNNER_UNIT_PREFIX = "actions.runner."
RUNNER_COMMS = frozenset({"runsvc.sh", "run.sh", "Runner.Listener"})


# A vnode runner account (`vnodehelper/layout.py::gha_user`: `vn<token>-gha<n>`).
_VNODE_GHA_USER_RE = re.compile(r"vn[a-z0-9]+-gha[0-9]+")
_UNIT_USER_RE = re.compile(r"^User=(\S{1,64})\s*$", re.MULTILINE)
SYSTEMD_ETC = Path("/etc/systemd/system")


def _is_vnode_runner_unit(unit: str) -> bool:
    """Whether a runner unit runs as a virtual node's runner account — another node's, possibly a tenant's,
    however its name reads (a vnode may be named `<host>-gha-<x>`, which the host's glob also matches). By
    the unit file's `User=`, the owner fact `vnodehelper/gha.py` keys on; an unreadable file is the host's."""
    try:
        text = (SYSTEMD_ETC / unit).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    owner = _UNIT_USER_RE.search(text)
    return bool(owner and _VNODE_GHA_USER_RE.fullmatch(owner.group(1)))


def _runner_line_admitted(record: dict) -> bool:
    """False for a runner unit's line that is not the host's runner's own stdout: a vnode's runner unit (by
    owner, or a writer in a vnode's uid range), a workflow's `logger` (`_TRANSPORT=syslog`), native journal
    writes, or a stream another process opened. systemd's own lines about a unit (`init.scope`, `UNIT=`) pass
    the owner check only. `_COMM` is the process's own to set, so this raises the bar rather than closing it;
    the CP reads these lines as claims."""
    own_unit = str(record.get("_SYSTEMD_UNIT") or "")
    about = str(record.get("UNIT") or "") if own_unit in ("", "init.scope") else own_unit
    if about.startswith(RUNNER_UNIT_PREFIX) and _is_vnode_runner_unit(about):
        return False
    if not own_unit.startswith(RUNNER_UNIT_PREFIX):
        return True
    try:
        if _is_vnode_uid(int(str(record.get("_UID") or "-1"))):
            return False
    except ValueError:
        return False
    return record.get("_TRANSPORT") == "stdout" and record.get("_COMM") in RUNNER_COMMS


@dataclass
class JournalRead:
    lines: List[bytes]
    returncode: Optional[int]  # None: journalctl could not be run at all
    stderr: bytes = b""
    oversized: int = 0  # lines past MAX_LINE_BYTES, skipped
    truncated: bool = False  # stopped at the line or byte limit: more remains after the last line


def read_journal(args: List[str], limit: int) -> JournalRead:
    """The first `limit` lines of `journalctl <args>` (fewer once `MAX_READ_BYTES` is read). Read by hand,
    not `-n`: `-n` keeps the NEWEST N, so a backlog past N after the cursor would be skipped — a gap.
    Complete lines read before a failure are returned with its returncode; they are in order, so the
    caller may still ship them."""
    try:
        proc = subprocess.Popen(["journalctl", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return JournalRead([], None, str(exc).encode())
    timer = threading.Timer(_JOURNALCTL_TIMEOUT_SECONDS, proc.kill)
    timer.start()
    lines: List[bytes] = []
    oversized = 0
    total = 0
    try:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline(MAX_LINE_BYTES)
            if not line:
                break
            if not line.endswith(b"\n") and len(line) >= MAX_LINE_BYTES:
                # Too big to hold: drain it to its newline and skip it (its cursor is inside it).
                while line and not line.endswith(b"\n"):
                    line = proc.stdout.readline(MAX_LINE_BYTES)
                oversized += 1
                continue
            lines.append(line)
            total += len(line)
            if len(lines) >= limit or total >= MAX_READ_BYTES:
                proc.kill()
                proc.wait()
                return JournalRead(lines, 0, b"", oversized, truncated=True)
        stderr = proc.stderr.read() if proc.stderr else b""
        return JournalRead(lines, proc.wait(), stderr, oversized)
    finally:
        timer.cancel()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def _realtime_iso(value: object) -> str:
    return datetime.fromtimestamp(int(str(value)) / 1e6, timezone.utc).isoformat()


def _message_text(value: object) -> str:
    """`MESSAGE` in each `-o json` spelling: a string; an array of byte values (a non-UTF-8 field); or an
    array of those (a field set more than once — joined, so a second value cannot blank the first)."""
    if value is None:
        return ""
    if isinstance(value, list):
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            try:
                return bytes(value).decode("utf-8", "replace")
            except (TypeError, ValueError):
                return ""
        return "\n".join(_message_text(item) for item in value)
    return str(value)


def parse_entry(line: bytes, admit: Optional[Callable[[dict], bool]] = None) -> Optional[tuple]:
    """`(cursor, wire entry)` for one `-o json` line, the message already redacted; None when unusable — a
    line without `__CURSOR` cannot advance the cursor, so it is skipped. `(cursor, None)` for a line `admit`
    refuses (it must not leave the node, but its cursor still advances)."""
    try:
        record = json.loads(line.decode("utf-8", "replace"))
        cursor = record["__CURSOR"]
        ts = _realtime_iso(record["__REALTIME_TIMESTAMP"])
    except (ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
        log.debug("node-logs: skipping an unparseable journal line (%s)", exc)
        return None
    if not isinstance(cursor, str) or not cursor:
        return None
    if admit is not None and not admit(record):
        return cursor, None
    unit = record.get("_SYSTEMD_UNIT") or ""
    # systemd's own lines about a unit ("Started …") come from init.scope; UNIT names the unit they concern.
    if not unit or unit == "init.scope":
        unit = record.get("UNIT") or unit
    try:
        priority = int(record.get("PRIORITY", DEFAULT_PRIORITY))
    except (TypeError, ValueError):
        priority = DEFAULT_PRIORITY
    # Any process in a unit can log `PRIORITY=99` over the native protocol; the CP takes 0..7 only.
    if not 0 <= priority <= 7:
        priority = DEFAULT_PRIORITY
    message = _wire_message(_message_text(record.get("MESSAGE")))
    return cursor, {"ts": ts, "unit": str(unit)[:MAX_UNIT_CHARS], "priority": priority, "message": message}


def _wire_message(text: str) -> str:
    # Redact the WHOLE message before truncating: a cut could split a secret past its pattern.
    message, _ = transcript_redact.redact_text(text)
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER
    return message


def parse_file_lines(chunk: bytes, offset: int, unit: str, limit: int, file_id: str = "") -> List[tuple]:
    """`(cursor, wire entry)` for each complete line of `chunk` (read from byte `offset`), at most `limit`; the
    cursor is the byte offset just past the line, then `:` and `file_id` (`file_cursor`). A trailing partial
    line waits for its newline — unless the chunk is all one line of `MAX_READ_BYTES` or more, which ships cut
    rather than wedging the file. A blank line, or a daemon record in `SELF_POSTED`, is `(cursor, None)`: it
    advances the cursor and ships nothing."""
    parsed: List[tuple] = []
    pos = 0
    while pos < len(chunk) and len(parsed) < limit:
        newline = chunk.find(b"\n", pos)
        if newline >= 0:
            end = newline + 1
        elif pos == 0 and len(chunk) >= MAX_READ_BYTES:
            end = len(chunk)
        else:
            break
        text = chunk[pos:end].decode("utf-8", "replace").rstrip("\r\n")
        pos = end
        cursor = file_cursor(offset + end, file_id)
        if not text.strip() or (unit == LOG_FILES[0][1] and any(record in text for record in SELF_POSTED)):
            parsed.append((cursor, None))
            continue
        head = _FILE_LINE_RE.match(text)
        stamp = datetime.now(timezone.utc)
        if head and head.group(1):
            try:
                at = datetime.fromisoformat(head.group(1))
                # `Z` is UTC; a bare stamp is the host's local time (`JaahService.cs`'s `DateTime.Now`).
                stamp = at.replace(tzinfo=timezone.utc) if head.group(2) else at.astimezone(timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
        level = head.group(3) if head else None
        priority = _LEVEL_PRIORITY.get(level or "", DEFAULT_PRIORITY)
        parsed.append((cursor, {"ts": stamp.isoformat(), "unit": unit[:MAX_UNIT_CHARS], "priority": priority,
                                "message": _wire_message(text)}))
    return parsed


def file_cursor(offset: int, file_id: str) -> str:
    return f"{offset}:{file_id}"


def parse_file_cursor(stored: Optional[str]) -> tuple:
    """`(offset, file id)` from a stored file cursor; `(None, "")` when absent or unreadable. A bare offset (a
    cursor written before ids were recorded) has id `""`, which matches any file."""
    if stored is None:
        return None, ""
    offset, _, file_id = stored.partition(":")
    try:
        return int(offset), file_id
    except ValueError:
        return None, ""


def file_id(st: os.stat_result) -> str:
    """The open file's identity: device and inode (Windows: volume serial and file index); `""` when the
    filesystem reports no inode."""
    return f"{st.st_dev}-{st.st_ino}" if st.st_ino else ""


def group_batches(parsed: List[tuple]) -> List[List[tuple]]:
    """Split `(cursor, entry)` pairs, in order, into groups whose wire body (`wire_batch`) json-encodes to
    ≤ MAX_BODY_BYTES."""
    groups: List[List[tuple]] = []
    current: List[tuple] = []
    size = 0
    for cursor, entry in parsed:
        entry_size = len(json.dumps(entry).encode()) + 2  # + the ", " separator
        overhead = len(json.dumps({"cursor": cursor, "entries": []}).encode())
        if current and size + entry_size + overhead > MAX_BODY_BYTES:
            groups.append(current)
            current, size = [], 0
        current.append((cursor, entry))
        size += entry_size
    if current:
        groups.append(current)
    return groups


def wire_batch(group: List[tuple]) -> dict:
    """The POST body for one group: its entries, and its LAST entry's cursor."""
    return {"cursor": group[-1][0], "entries": [entry for _cursor, entry in group]}


# `_post_batch` outcomes. REJECTED is the CP refusing THIS body (413/422): resending it unchanged would
# wedge shipping behind it forever, so it is split, and a lone refused entry is replaced or skipped.
_OK, _RETRY, _REJECTED = "ok", "retry", "rejected"
_REJECTING_STATUSES = (400, 413, 422)
REFUSED_PLACEHOLDER = "[node-logs: the control plane refused this entry (HTTP {code}); not shipped]"


class NodeLogForwarder:
    """One `PeriodicStep`: drain the journal after the persisted cursor, POST, advance on acknowledgement."""

    def __init__(
        self,
        *,
        read: Callable[[List[str], int], JournalRead] = read_journal,
        post: Callable[[dict], dict] | None = None,
        clock: Callable[[], float] = time.monotonic,
        cursor_path: Path | None = None,
        kernel_cursor_path: Path | None = None,
        environ: Mapping[str, str] = os.environ,
        gha_unit_glob: Callable[[], Optional[str]] = host_gha_unit_glob,
    ) -> None:
        self._read = read
        self._post = post or control_plane_api_mod.agent_api_node_logs
        self._clock = clock
        self._cursor_path = cursor_path or env_helpers.daemon_state_dir("node-logs.cursor")
        # Beside the units' cursor: a kernel cursor is a position in a different match, never interchangeable.
        self._kernel_cursor_path = kernel_cursor_path or self._cursor_path.with_name("node-logs-kernel.cursor")
        self._gha_unit_glob = gha_unit_glob
        self._environ = environ
        self._backoff_until: Optional[float] = None
        self._warned_unreadable = False
        self._warned_files: set = set()

    def load_cursor(self, path: Path | None = None) -> Optional[str]:
        try:
            return (path or self._cursor_path).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _store_cursor(self, cursor: str, path: Path | None = None) -> None:
        path = path or self._cursor_path
        path.parent.mkdir(parents=True, exist_ok=True)
        env_helpers.atomic_write_text(path, cursor, mode=0o600, fsync=True)

    def _journal_args(self, cursor: Optional[str]) -> List[str]:
        # `--all`: without it `-o json` prints `null` for any field over 4096 bytes — a long line shipped empty.
        args = ["-o", "json", "--all", "--no-pager"]
        gha = self._gha_unit_glob()
        for unit in (*UNITS, gha) if gha else UNITS:
            args += ["-u", unit]
        args.append(f"--after-cursor={cursor}" if cursor else f"--since={FIRST_RUN_SINCE}")
        return args

    def _kernel_args(self, cursor: Optional[str]) -> List[str]:
        # A field match, not `-k`: `-k` also narrows to the current boot, and an OOM before a reboot is the
        # one worth seeing.
        args = ["-o", "json", "--all", "--no-pager", "_TRANSPORT=kernel"]
        args.append(f"--after-cursor={cursor}" if cursor else f"--since={FIRST_RUN_SINCE}")
        return args

    def pass_once(self) -> int:
        """One pass over both sources, synchronously. Returns the number of batches the CP acknowledged."""
        # A vnode daemon has no access to the shared host journal and must not read its neighbours'
        # (deferred).
        if env_get("JAAH_VNODE", None, self._environ) == "1":
            return 0
        if self._backoff_until is not None:
            if self._clock() < self._backoff_until:
                return 0
            self._backoff_until = None
        if not host_platform.is_linux():
            return self._pass_files()
        posted = self._pass_source(self._cursor_path, self._journal_args, None, _runner_line_admitted)
        # A backoff the units' POSTs earned (403/503) holds for the kernel's too: the same CP answered.
        if self._backoff_until is None:
            posted += self._pass_source(self._kernel_cursor_path, self._kernel_args, _is_kernel_oom,
                                        _kernel_admitted)
        return posted

    def _pass_source(
        self,
        cursor_path: Path,
        args_for: Callable[[Optional[str]], List[str]],
        keep: Optional[Callable[[dict], bool]],
        admit: Callable[[dict], bool],
    ) -> int:
        """Drain one journal source after its own cursor. `admit` filters raw journal records (`parse_entry`);
        `keep` (the kernel source) filters entries, which then ship as `unit: "kernel"`. A read with nothing
        left to ship advances the cursor locally."""
        posted = 0
        reads = 0
        while posted < MAX_BATCHES_PER_PASS and reads < MAX_BATCHES_PER_PASS:
            reads += 1
            cursor = self.load_cursor(cursor_path)
            result = self._read(args_for(cursor), MAX_ENTRIES_PER_READ)
            parsed = [p for p in (parse_entry(line, admit) for line in result.lines) if p is not None]
            if result.oversized:
                log.warning("node-logs: skipped %d journal line(s) over %d bytes", result.oversized, MAX_LINE_BYTES)
            if result.returncode != 0 and not parsed:
                self._journal_failed(result, cursor, cursor_path)
                return posted
            if result.returncode == 0 and not result.lines:
                self._note_empty_read(result)
                return posted
            self._warned_unreadable = False
            if not parsed:
                # Nothing that can advance the cursor: re-reading would return the same lines forever.
                return posted
            shipped = [(c, e) for c, e in parsed if e is not None and (keep is None or keep(e))]
            if keep is not None:
                shipped = [(c, dict(e, unit=KERNEL_UNIT)) for c, e in shipped]
            if not shipped:
                # Nothing here leaves the node, so nothing can be lost by moving past it without the CP.
                self._store_cursor(parsed[-1][0], cursor_path)
            # Lines read before a failure are in order, so they ship; the failure is reported after.
            for group in group_batches(shipped):
                if posted >= MAX_BATCHES_PER_PASS or not self._deliver(group, cursor_path):
                    return posted
                posted += 1
            if result.returncode != 0:
                # The cursor moved past what shipped, so this failure is not the saved cursor's to reset.
                self._journal_failed(result, None, cursor_path)
                return posted
            if not result.truncated and len(result.lines) < MAX_ENTRIES_PER_READ:
                return posted
        return posted

    def _pass_files(self) -> int:
        """Off Linux: tail each of `LOG_FILES` after its own byte offset, posting as `_pass_source` does."""
        log_dir = host_platform.paths().log_dir
        posted = 0
        # Round-robin: each round splits what is left evenly among the files that used their whole share
        # last round; one that ran dry drops out, so its share goes to the busier ones.
        active = list(LOG_FILES) if log_dir else []
        while active and self._backoff_until is None and posted < MAX_BATCHES_PER_PASS:
            share = max(1, (MAX_BATCHES_PER_PASS - posted) // len(active))
            busy = []
            for name, unit in active:
                if self._backoff_until is not None or posted >= MAX_BATCHES_PER_PASS:
                    break
                stem = name.rsplit(".", 1)[0]
                got = self._pass_file(Path(log_dir) / name, unit,
                                      self._cursor_path.with_name(f"node-logs-file-{stem}.cursor"),
                                      min(share, MAX_BATCHES_PER_PASS - posted))
                posted += got
                if got >= share:
                    busy.append((name, unit))
            active = busy
        return posted

    def _pass_file(self, path: Path, unit: str, cursor_path: Path, budget: int) -> int:
        posted = 0
        reads = 0
        while posted < budget and reads < MAX_BATCHES_PER_PASS:
            reads += 1
            try:
                offset, chunk, ident = self._read_file(path, cursor_path)
            except FileNotFoundError:
                return posted  # the service has not run yet
            except OSError as exc:
                if path not in self._warned_files:
                    log.warning("node-logs: %s unreadable (%s); not shipped", path, exc)
                    self._warned_files.add(path)
                return posted
            self._warned_files.discard(path)
            parsed = parse_file_lines(chunk, offset, unit, MAX_ENTRIES_PER_READ, ident)
            if not parsed:
                return posted
            shipped = [(c, e) for c, e in parsed if e is not None]
            if not shipped:
                self._store_cursor(parsed[-1][0], cursor_path)
            for group in group_batches(shipped):
                if posted >= budget or not self._deliver(group, cursor_path):
                    return posted
                posted += 1
            if len(chunk) < MAX_READ_BYTES and len(parsed) < MAX_ENTRIES_PER_READ:
                return posted
        return posted

    def _read_file(self, path: Path, cursor_path: Path) -> tuple:
        """`(offset, bytes from it, file id)`, at most `MAX_READ_BYTES`. The offset is the stored cursor's; with
        none, the first line start within `FIRST_RUN_TAIL_BYTES` of the end; past the end (truncated) or on another
        file than the cursor's (replaced), 0. A start chosen here is stored at once: nothing before it will be
        shipped, so nothing waits on the CP."""
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            size, ident = st.st_size, file_id(st)
            offset, stored_id = parse_file_cursor(self.load_cursor(cursor_path))
            replaced = bool(stored_id and ident and stored_id != ident)
            if offset is None or replaced or not 0 <= offset <= size:
                offset = max(0, size - FIRST_RUN_TAIL_BYTES) if offset is None else 0
                if offset:
                    # Mid-line: begin after the next newline (the byte before `offset` may itself be one).
                    fh.seek(offset - 1)
                    newline = fh.read(FIRST_RUN_TAIL_BYTES + 1).find(b"\n")
                    offset = offset + newline if newline >= 0 else size
                self._store_cursor(file_cursor(offset, ident), cursor_path)
            fh.seek(offset)
            return offset, fh.read(MAX_READ_BYTES), ident

    def _note_empty_read(self, result: JournalRead) -> None:
        # journalctl exits 0 with nothing but this on stderr when it may not open the journal at all.
        stderr = result.stderr.decode("utf-8", "replace").strip()
        if "insufficient permissions" in stderr.lower() and not self._warned_unreadable:
            log.warning("node-logs: journal unreadable (%s); jaahd needs the systemd-journal group", stderr)
            self._warned_unreadable = True

    def _journal_failed(self, result: JournalRead, cursor: Optional[str], cursor_path: Path) -> None:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        if result.returncode is not None and cursor and "cursor" in stderr.lower():
            log.warning("node-logs: journalctl rejected the saved cursor (%s); restarting from %s",
                        stderr, FIRST_RUN_SINCE)
            try:
                cursor_path.unlink()
            except OSError:
                pass
            return
        if not self._warned_unreadable:
            log.warning("node-logs: journal unreadable (rc=%s: %s); jaahd needs the systemd-journal group",
                        result.returncode, stderr)
            self._warned_unreadable = True

    def _deliver(self, group: List[tuple], cursor_path: Path) -> bool:
        """POST one group; True once the cursor has advanced past all of it."""
        outcome, code = self._post_batch(wire_batch(group), cursor_path)
        if outcome != _REJECTED:
            return outcome == _OK
        if len(group) > 1:
            mid = len(group) // 2
            return self._deliver(group[:mid], cursor_path) and self._deliver(group[mid:], cursor_path)
        cursor, entry = group[0]
        log.warning("node-logs: the CP refused one journal entry (HTTP %s); shipping a placeholder", code)
        placeholder = dict(entry, priority=3, message=REFUSED_PLACEHOLDER.format(code=code))
        outcome, code = self._post_batch({"cursor": cursor, "entries": [placeholder]}, cursor_path)
        if outcome != _REJECTED:
            return outcome == _OK
        # A fixed placeholder refused too means the refusal is systemic (a proxy, a CP fault), not this entry:
        # skipping would open a gap in the trail, so back off with the cursor kept.
        log.error("node-logs: the CP refused even the placeholder (HTTP %s); backing off %ss with the cursor kept",
                  code, int(BACKOFF_SECONDS))
        self._backoff_until = self._clock() + BACKOFF_SECONDS
        return False

    def _post_batch(self, batch: dict, cursor_path: Path) -> tuple:
        """`(outcome, http status or None)`; on `_OK` the cursor is already stored."""
        try:
            response = self._post(batch)
        except urllib.error.HTTPError as exc:
            if exc.code in _REJECTING_STATUSES:
                return _REJECTED, exc.code
            if exc.code in (403, 503):
                log.warning("node-logs: CP answered %s; backing off %ss with the cursor kept",
                            exc.code, int(BACKOFF_SECONDS))
                self._backoff_until = self._clock() + BACKOFF_SECONDS
            else:
                log.warning("node-logs: POST failed (HTTP %s); retrying next pass", exc.code)
            return _RETRY, exc.code
        except Exception as exc:  # noqa: BLE001 — any transport failure: retry the same batch next pass
            log.warning("node-logs: POST failed (%s); retrying next pass", exc)
            return _RETRY, None
        acked = response.get("cursor") if isinstance(response, dict) else None
        if acked != batch["cursor"]:
            log.warning("node-logs: CP acknowledged cursor %r, expected the batch's last; not advancing", acked)
            return _RETRY, None
        self._store_cursor(acked, cursor_path)
        return _OK, None

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)
