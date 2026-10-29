"""Process inspection — `/proc` reads, redacted command lines and the cwd scan that says who is live in a slot."""
from __future__ import annotations

import errno
import logging
import os
import re
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

import env_helpers
import host_platform
import session_user

log = logging.getLogger("jaahd.worktree_mechanics.processes")


# Sentinel from the cwd scan when liveness is UNKNOWN (scan crashed, or a cwd unreadable even as the
# session user). `fleet_launch` REFUSES on it; `reconcile.observe.observe_slot` withholds the observation.
PROBE_FAILED = "__PROBE_FAILED__"


# ── The delegated-call timeout ─────────────────────────────────────────────────────────────────────────
# The cwd resolve and the reap's `kill` delegate to the session user: a `tmux run-shell` round trip (tens
# of ms). This caps the command, not the switch.
_SESSION_PROBE_TIMEOUT_S = 30.0


# ── The /proc reads, behind three names ────────────────────────────────────────────────────────────────
# The process table's whole surface (with `_read_cmdline`, `_proc_owner_uids`) — the test seams for gate 0.


def _proc_pids() -> list[int]:
    """Every numeric entry under /proc."""
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def _proc_ppid(pid: int) -> int | None:
    """PID's parent from `/proc/<pid>/stat`, or None when it cannot be read.

    PPid is field 4; `comm` (field 2) may contain spaces and parens, so the split is after the LAST ')'."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
        return int(stat.rsplit(b")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _proc_cwd(pid: int) -> str:
    """`/proc/<pid>/cwd`, RAISING `OSError` exactly as `os.readlink` does — the errno is the answer here
    (ENOENT is a raced exit, EACCES is "ask the owner"), so it must not be flattened into a return value."""
    return os.readlink(f"/proc/{pid}/cwd")


def _proc_owner_uids(pid: int) -> tuple[int, ...]:
    """The (real, effective) uid of PID from `/proc/<pid>/status`, or `()` when it cannot be read.

    `status` is `0444` for EVERY process whoever owns it, while its `cwd` symlink is not. That asymmetry
    is why gate 0 can tell "I may not look at this one" apart from "this one just exited"."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return tuple(int(x) for x in line.split()[1:3])
    except (OSError, ValueError):
        return ()
    return ()


def _boot_time_epoch() -> float | None:
    """The host's boot instant as a UTC epoch, from `/proc/stat`'s `btime` line. None when unreadable."""
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


# 1970-01-01 in FILETIME units (100ns since 1601-01-01), the origin `host_platform.start_time` counts from
# on Windows.
_FILETIME_EPOCH = 116_444_736_000_000_000


def proc_started_at(pid: object) -> str:
    """PID's kernel-recorded start instant as a UTC ISO stamp, or "" when it cannot be read.

    **THE OTHER HALF OF A RUNNER'S IDENTITY.** A pid is a recycled number, so a CP holding a lease against
    a bare one would read a stranger as its live runner and never reclaim the slot. `/proc/<pid>/stat`
    field 22 is the start time in ticks since boot, resolved here against `/proc/stat`'s `btime` so what
    ships is an ABSOLUTE instant that survives the reboot resetting the tick origin.

    `comm` can contain spaces and parentheses, so fields are split after the LAST `)`. Any failure returns
    "" — "we could not tell", which the CP must not read as an identity match.

    Lives here, not in `reconcile/`, because `reap_orphan_processes` re-asserts it before a SIGKILL and
    this is the lower module; `reconcile.context.proc_started_at` re-exports it.

    ON WINDOWS the instant is the process's creation FILETIME (`host_platform.start_time`) — already
    absolute, no boot origin to resolve — and PID must be the Windows pid (a pane's MSYS `#{pane_pid}`
    goes through `host_platform.pane_os_pid` first, at the caller).

    ON MACOS it is the kernel's own start instant (`host_platform.start_time`, microseconds since the
    epoch) — absolute too, so there is no procfs and no boot time to read."""
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return ""
    if host_platform.is_windows():
        created = host_platform.start_time(pid_i)
        if not created:
            return ""
        epoch = (created - _FILETIME_EPOCH) / 10_000_000
        return env_helpers.now_iso(epoch)
    if host_platform.is_macos():
        started = host_platform.start_time(pid_i)
        if not started:
            return ""
        return env_helpers.now_iso(started / 1_000_000)
    try:
        with open(f"/proc/{pid_i}/stat") as fh:
            raw = fh.read()
        fields = raw[raw.rindex(")") + 1:].split()
        ticks = float(fields[19])  # field 22 overall; 20th after the (pid, comm) pair
    except (OSError, ValueError, IndexError):
        return ""
    btime = _boot_time_epoch()
    if btime is None:
        return ""
    hz = os.sysconf("SC_CLK_TCK") or 100
    return env_helpers.now_iso(btime + ticks / hz)


# ── Gate 0's NON-RUNNER DENY-LIST — ONE entry: a tmux socket under THIS HOST'S REAL pytest temp root ───
# Anchored at both ends (a deny-list entry licenses ignoring a LIVE occupant, and a process writes its own
# argv): this host's real temp root, `tmux.sock`, whole-argument boundaries. A `pytest-of-*` dir forged
# INSIDE a pool worktree buys no slot veto.
_NON_RUNNER_CMDLINE_PATTERN = (
    r"(?:^|\s)" + re.escape(tempfile.gettempdir().rstrip("/"))
    + r"/pytest-of-[^/\s]+/pytest-[0-9]+/(?:[^/\s]+/)*tmux\.sock(?:\s|$)"
)
# ── SECOND ENTRY: LOGIN-SESSION PLUMBING, which is unreadable BY CONSTRUCTION and never an occupant ────
# `(sd-pam)` and `tailscaled be-child ssh` run as the session user but are forked by root, so under Yama
# `ptrace_scope=1` their `/proc/<pid>/cwd` is EACCES even to that user (measured) — one would blank every
# slot's verdict for a whole SSH session. Safe to deny: both exist before any shell and never sit in a
# worktree. Anchored at argv[0] (a runner cannot forge an exec of that name); `be-child ssh` in order.
_NON_RUNNER_CMDLINE_PATTERN += (
    r"|^\(sd-pam\)\s*$"
    r"|^(?:\S*/)?tailscaled\s+be-child\s+ssh(?:\s|$)"
)
_NON_RUNNER_CMDLINE_RE = re.compile(_NON_RUNNER_CMDLINE_PATTERN)


def _is_non_runner_process(pid: int) -> bool:
    """True when PID's cmdline marks it as TEST-HARNESS machinery gate 0 must not report as an occupant —
    an orphaned pytest tmux server (`tmux -S /tmp/pytest-of-*/pytest-*/.../tmux.sock`) rooted in a pool
    worktree, which `reconcile/observe.py` folds into `worktrees.session` and `app/slot_state.py::lease` reads as
    "never assign this slot".

    A DENY-LIST, not an allow-list: an allow-list fails CLOSED on an unknown occupant, resetting a live
    tree. This fails OPEN, and nothing bounds that: the free-slot reap requires `worktrees.session`, set
    by the agents probe alone, so a stray tmux server is only collected by the next release's `_clear_tree`.

    `cmdline` is `0444`, so this runs BEFORE the cwd read and covers both readers. Unreadable is NOT a
    deny; only a positive match excludes."""
    cmdline = _read_cmdline(pid)
    if cmdline is None:
        return False
    return bool(_NON_RUNNER_CMDLINE_RE.search(cmdline))


def _read_cmdline(pid: int) -> str | None:
    """PID's argv as one space-joined string, or None when `/proc/<pid>/cmdline` cannot be read.

    None is UNREADABLE, `""` is a kernel thread (empty cmdline) — the two callers want them apart: the
    deny-list must not deny on an unreadable one, and the identification below must not claim a name it
    never read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None


# ── Identifying the occupants, not merely counting them ────────────────────────────────────────────────
# Which processes, not just how many — bounded and REDACTED here, the only place that sees the raw argv
# (it lands in the `worktrees.probes` jsonb). Three shapes: `--flag=secret`, bare `KEY=value`
# (`env ANTHROPIC_API_KEY=… claude`), URL userinfo. NOT a guarantee — a positional secret is
# indistinguishable from a filename; the length bound is the backstop.
REDACTED = "<redacted>"
UNREADABLE_CMD = "<cmdline unreadable>"
_SECRET_WORDS = r"token|secret|password|passwd|api[-_]?key|auth|credential|cookie|bearer|pgpassword"
_SECRET_ARG_RE = re.compile(
    rf"(?i)(--?[\w-]*(?:{_SECRET_WORDS})[\w-]*)(=|\s+)(\S+)"
)
# The bare `KEY=value` form, as `env` and a shell prefix both spell it — anchored to a whole argument so a
# `--flag=…` already handled above is not re-matched mid-string.
_SECRET_ENV_RE = re.compile(
    rf"(?i)(?:(?<=^)|(?<=\s))([\w.]*(?:{_SECRET_WORDS})[\w.]*)(=)(\S+)"
)
# `scheme://user:password@host` — the password half only; the user and host still identify the process.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s@]+)@")
# Long enough to identify a runner, short enough that a dozen stay readable.
_CMD_SUMMARY_MAX = 200
# `matched` carries the TRUE total, so a capped list never reads as the whole population.
_CWD_PROCESS_LIMIT = 25


def _cmd_summary(cmdline: str) -> str:
    """One process's argv, redacted and length-bounded, safe to put in a DB row and a console Task body.

    NEWLINES COLLAPSE TO SPACES: the delegated session-user scan below reports one process per LINE, so an
    argv carrying a literal newline would otherwise forge extra records in that protocol."""
    flat = " ".join(cmdline.split())
    redacted = _SECRET_ARG_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", flat)
    redacted = _SECRET_ENV_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", redacted)
    redacted = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}:{REDACTED}@", redacted)
    if len(redacted) <= _CMD_SUMMARY_MAX:
        return redacted
    return redacted[: _CMD_SUMMARY_MAX - 1] + "…"


# ── REPEATING FAULTS: SAY IT ONCE PER FACT, NOT ONCE PER PASS ──────────────────────────────────────────
#
# Standing faults (an unreadable cwd) would re-log every ~5s tick and bury the journal. Deduped per FACT:
# key = which report about which tree, fingerprint = the pid set. Unchanged → DEBUG; changed → full volume;
# cleared when the fault goes. Re-asserted hourly so a never-clearing fault stays findable after
# rotation and visible to error-rate alerting.
_FAULT_LOCK = threading.Lock()
#: How long a standing fault stays quiet before it is re-stated at full volume.
_FAULT_REASSERT_SECS = 3600.0
#: report key -> (the fingerprint last reported at full volume, the monotonic time it was reported).
_REPORTED_FAULTS: dict[str, tuple[object, float]] = {}


def _fault_is_new(key: str, fingerprint: object) -> bool:
    """Whether KEY's fault should be reported at full volume, recording FINGERPRINT as reported when so.

    True the first time, whenever the fingerprint CHANGES, and once per `_FAULT_REASSERT_SECS` while an
    unchanged fault still stands; False in between."""
    now = time.monotonic()
    with _FAULT_LOCK:
        seen = _REPORTED_FAULTS.get(key)
        if seen is not None and seen[0] == fingerprint and (now - seen[1]) < _FAULT_REASSERT_SECS:
            return False
        _REPORTED_FAULTS[key] = (fingerprint, now)
        return True


def _fault_cleared(key: str) -> None:
    """Forget KEY's last report, so its next occurrence is announced as a first one."""
    with _FAULT_LOCK:
        _REPORTED_FAULTS.pop(key, None)


@dataclass(frozen=True, slots=True)
class CwdScan:
    """Gate 0's answer: the verdict every caller has always had, plus WHO was found.

    `status` is the original tri-state contract — `"1"` live, `""` clear, `PROBE_FAILED` unknown — and
    `has_live_cwd_process` still returns exactly it, so no kill-safety caller changes.

    `processes` is `{"pid": int, "cmd": str}` per match, capped at `_CWD_PROCESS_LIMIT`; `matched` is the
    true total. EMPTY IS NOT "NONE FOUND": a `status == "1"` scan run with `collect=False` short-circuits
    on the first hit and reports no processes at all, and PROBE_FAILED never enumerates."""

    status: str
    processes: tuple[dict[str, object], ...] = ()
    matched: int = 0


# The scan the SESSION USER runs for pids we cannot read. Tiny: the exclusion walk and fail-closed decision
# stay here; only the syscall needing the other uid crosses. It re-applies the deny-list (it sees exactly
# the pids the daemon could not). A raced exit is a skip whatever errno (`still_there`).
# Line protocol, verdict LAST: `HIT <pid> <argv>` lines, then MATCH / CLEAR / "UNKNOWN <pids>". `mode` is
# `all` (enumerate, for the report) or `first` (short-circuit, for the kill-safety gate).
_CWD_RESOLVE_PROGRAM = r"""
import errno, os, re, sys
pattern, mode, nwt = sys.argv[1], sys.argv[2], int(sys.argv[3])
wts = sys.argv[4:4 + nwt]
deny = re.compile(pattern)
unknown = []
hit = False
# A PID THE KERNEL REFUSED IS NOT AUTOMATICALLY A PID NOBODY CAN READ. `/proc/<pid>/cwd` only reads ENOENT
# for a task that is gone AND whose dentry has been revalidated away; a task that vanishes under the read
# itself trips `proc_pid_get_link`'s guard, whose error is initialised to -EACCES and returned unchanged
# when the lookup of the task comes back NULL. So a raced exit reaches this handler as EACCES, went into
# `unknown`, and failed the WHOLE slot closed on one dead pid. Measured 2026-08-25, prod node h2 — one such
# line per slot, all 30, ~1s apart, on EVERY observation pass:
#   gate 0: 434 cwd(s) stayed unreadable even as jaah (UNKNOWN 2770366)
#           — the live-cwd probe is UNRELIABLE for <worktree path>
# /proc/2770366 did not exist when the pid was checked on the node: it had exited between the daemon's
# enumeration and this read. h2's pass ran 15-35s against h1's <=15s for 10 slots, all of it this.
#
# SKIPPING A DEAD PID DOES NOT WEAKEN THE GATE, and that is the whole security argument. What gate 0 fails
# CLOSED on is liveness that is genuinely UNKNOWABLE — nobody on this host can say whether a live process
# sits in that tree. A process that no longer exists is not unknowable in that sense: it cannot be cwd'd
# inside a worktree it has left, so declining to count it removes no uncertainty the gate was protecting.
# Only a PERMISSION failure on a pid that is STILL THERE belongs in UNKNOWN. Treating every EACCES as a
# skip was rejected outright — that is the fail-OPEN bug this delegation exists to prevent, since the
# fleet's own tmux server has an unreadable cwd BY CONSTRUCTION.
#
# `/proc/<pid>` is 0555 for every process alive and gone the instant one is reaped — the same asymmetry
# `_proc_owner_uids` leans on daemon-side, where reading the 0444 `status` file is what tells "I may not
# look at this one" apart from "this one just exited", and it is why the local half never carried this
# bug. Any OTHER stat errno keeps the pid in UNKNOWN: unexplained is still unknowable, and this branch
# must fail closed on it.
def still_there(pid):
    try:
        os.stat("/proc/" + pid)
    except OSError as exc:
        return exc.errno not in (errno.ENOENT, errno.ESRCH)
    return True
for pid in sys.argv[4 + nwt:]:
    cmdline = ""
    try:
        with open("/proc/" + pid + "/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if deny.search(cmdline):
            continue          # test-harness machinery, not an occupant (see the deny-list note)
    except OSError:
        pass                  # unreadable cmdline is NOT a deny; fall through to the cwd read
    try:
        target = os.readlink("/proc/" + pid + "/cwd")
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM) and still_there(pid):
            unknown.append(pid)   # a REFUSAL on a live pid — the one case nobody on this host can settle
        continue
    # EVERY worktree this pid sits under, not the first: a pool tree nested inside a project's main clone
    # is under BOTH, and the daemon-side half has always counted such a pid for both trees. Reporting only
    # the innermost would make the delegated half disagree with the local one for exactly those pids.
    matched_any = False
    for i, wt in enumerate(wts):
        if target == wt or target.startswith(wt + "/"):
            matched_any = True
            hit = True
            # The daemon bounds and REDACTS this; sending it raw keeps one such rule instead of two.
            print("HIT " + str(i) + " " + pid + " " + " ".join(cmdline.split()))
    if matched_any and mode != "all":
        break
if hit:
    print("MATCH")
else:
    print("UNKNOWN " + " ".join(unknown) if unknown else "CLEAR")
"""


def _parse_resolved_hits(lines: list[str], count: int) -> list[tuple[list[dict[str, object]], int]]:
    """The `HIT <wt index> <pid> <argv>` lines of the delegated scan, split per worktree: for each of the
    `count` trees asked about, its capped records plus its true match count.

    A malformed line is DROPPED rather than raised on: this parses the output of a program run as another
    uid, and an unparseable record must cost the detail, never the verdict the caller actually gates on.
    An out-of-range index is malformed in exactly that sense and is dropped the same way."""
    per_tree: list[tuple[list[dict[str, object]], int]] = [([], 0) for _ in range(count)]
    for line in lines:
        if not line.startswith("HIT "):
            continue
        _tag, _sep, rest = line.partition(" ")
        index_text, _sep, rest = rest.partition(" ")
        pid_text, _sep, cmdline = rest.partition(" ")
        try:
            index = int(index_text)
            pid = int(pid_text)
        except ValueError:
            continue
        if not 0 <= index < count:
            continue
        processes, matched = per_tree[index]
        per_tree[index] = (processes, matched + 1)
        if len(processes) < _CWD_PROCESS_LIMIT:
            # An empty argv from the child is a cmdline IT could not read — the same fact the local half
            # renders as `<cmdline unreadable>`, and rendering it as an empty backtick pair instead would
            # put a nameless bullet in the operator's list.
            processes.append({
                "pid": pid,
                "cmd": _cmd_summary(cmdline) if cmdline.strip() else UNREADABLE_CMD,
            })
    return per_tree



def _safe_verdict(line: str) -> str:
    """A delegated-scan line, reduced to something safe to log: a recognised verdict verbatim, anything
    else described rather than quoted.

    The child prints occupant argv UNREDACTED — deliberately, so redaction has one home on this side — and
    the "last line" the parser reads is one of those whenever the run is cut short. Echoing it into the
    journal would defeat `_cmd_summary` entirely."""
    if line == "MATCH" or line == "CLEAR" or line.startswith("UNKNOWN "):
        return line
    return f"<unrecognised, {len(line)} chars>"


def _gate0_probe_key(where: str) -> str:
    """The ONE fault key every failure verdict of the delegated cwd probe reports under.

    Shared by all four PROBE_FAILED arms, so flipping between them is one changed fingerprint and one
    answered pass clears all. The multi form's `where` collapses to "N pool tree(s)" — harmless (the pid
    set is tree-independent), but nothing keyed on a PATH may be added to it."""
    return f"gate0-probe:{where}"


def resolve_cwds_as_session_user_multi(
    wt_reals: list[str], pids: list[int], *, collect: bool = True,
) -> list[CwdScan]:
    """Ask the SESSION USER, IN ONE CROSSING, which of WT_REALS each of PIDS sits in: a `CwdScan` per tree,
    positionally.

    These are the session-user pids gate 0 could not read. Skipping them is fail-OPEN (every runner is one);
    unknown-without-asking fails closed forever (the fleet's tmux server is unreadable by construction). Any
    failure, or pids STILL unreadable, is PROBE_FAILED for every tree.

    One crossing for the whole pool: the deferred pid set does not vary by tree. `collect=False`
    short-circuits on the first hit and is only passed for a single tree."""
    if not wt_reals:
        return []
    where = wt_reals[0] if len(wt_reals) == 1 else f"{len(wt_reals)} pool tree(s)"
    if not session_user.can_run_as_session_user():
        # One fault key for every verdict (`_gate0_probe_key`).
        speak = (log.warning if _fault_is_new(_gate0_probe_key(where), ("no-switch", len(pids)))
                 else log.debug)
        speak(
            "gate 0: %s process(es) owned by the session user (%s) have cwds this process (uid %s) may not "
            "read, and the switch is unavailable (no fleet socket / no exec spool) — reporting the live-cwd "
            "probe as UNRELIABLE for %s rather than reading a permission error as an empty worktree. "
            "Check `jaah-runner@<node name>.service`.",
            len(pids), session_user.session_user(), os.geteuid(), where,
        )
        return [CwdScan(PROBE_FAILED) for _ in wt_reals]
    # Pattern and tree count ride as ARGV values, never templated into source executed as another uid;
    # the count separates paths from pids without a delimiter a path could contain.
    argv = [sys.executable or "python3", "-c", _CWD_RESOLVE_PROGRAM,
            _NON_RUNNER_CMDLINE_PATTERN, "all" if collect else "first", str(len(wt_reals)),
            *wt_reals, *[str(p) for p in pids]]
    try:
        proc = session_user.run_as_session_user(argv, timeout=_SESSION_PROBE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        speak = (log.warning if _fault_is_new(_gate0_probe_key(where), ("switch-failed", str(exc)))
                 else log.debug)
        speak("gate 0: could not resolve %s unreadable cwd(s) as %s for %s: %s",
              len(pids), session_user.session_user(), where, exc)
        return [CwdScan(PROBE_FAILED) for _ in wt_reals]
    lines = [line.strip() for line in (proc.stdout or "").strip().splitlines()]
    verdict = lines[-1] if lines else ""
    if proc.returncode != 0 or not verdict:
        # A run killed mid-scan ends on a raw `HIT` argv line; only a recognised verdict is echoed.
        speak = (log.warning if _fault_is_new(_gate0_probe_key(where), ("rc", proc.returncode, verdict))
                 else log.debug)
        speak("gate 0: the delegated cwd scan for %s answered rc=%s %r (stderr: %s)",
              where, proc.returncode, _safe_verdict(verdict),
              (proc.stderr or "").strip()[:200])
        return [CwdScan(PROBE_FAILED) for _ in wt_reals]
    # An answered pass clears the fault.
    if verdict in ("MATCH", "CLEAR"):
        _fault_cleared(_gate0_probe_key(where))
    if verdict == "MATCH":
        # Per tree off the HIT lines: `MATCH` is global, so a tree with no line is genuinely clear.
        return [
            CwdScan("1", tuple(processes), matched) if matched else CwdScan("")
            for processes, matched in _parse_resolved_hits(lines, len(wt_reals))
        ]
    if verdict == "CLEAR":
        return [CwdScan("") for _ in wt_reals]
    # A standing fault: once per pid set. Fingerprinted on the RAW verdict and the printed count, never
    # the rendering (distinct verdicts collapse to one `<unrecognised, N chars>` text).
    speak = (log.warning if _fault_is_new(_gate0_probe_key(where), ("unknown", len(pids), verdict))
             else log.debug)
    speak("gate 0: %s cwd(s) stayed unreadable even as %s (%s) — the live-cwd probe is UNRELIABLE "
          "for %s", len(pids), session_user.session_user(), _safe_verdict(verdict), where)
    return [CwdScan(PROBE_FAILED) for _ in wt_reals]


def resolve_cwds_as_session_user(wt_real: str, pids: list[int], *, collect: bool = True) -> CwdScan:
    """The one-tree form of `resolve_cwds_as_session_user_multi`, kept because it is the shape every
    kill-safety caller injects in its tests and the shape `scan_cwd_processes` documents."""
    return resolve_cwds_as_session_user_multi([wt_real], pids, collect=collect)[0]



def _merge_scans(local: CwdScan, delegated: CwdScan) -> CwdScan:
    """Fold the delegated session-user half of gate 0 into the locally-scanned half.

    THE TWO POPULATIONS MERGE — the delegated one cannot simply replace the local one, because a tree can
    hold both a readable occupant and an unreadable session-user pid and `collect` mode owes the caller
    every occupant, not the last scan's half.

    **THE VERDICT IS NEVER DERIVED FROM A COUNT.** `CwdScan("1")` with no records is how this module spells
    a bare occupied answer, so reading `matched` as the verdict turned an occupied delegated half into
    `""` — opening every kill gate downstream. UNKNOWN likewise beats a partial yes only when there is no
    yes: a match already in hand is a fact the failed half cannot unmake."""
    if delegated.status == PROBE_FAILED:
        return local if local.status == "1" else CwdScan(PROBE_FAILED)
    if delegated.status != "1":
        return local
    room = max(0, _CWD_PROCESS_LIMIT - len(local.processes))
    return CwdScan(
        "1",
        local.processes + delegated.processes[:room],
        # `max(1, ...)`: an occupied verdict carrying no parseable records still found ONE occupant, and
        # the count must never contradict the verdict beside it.
        local.matched + max(1, delegated.matched),
    )


def has_live_cwd_process(
    wt: str,
    caller_pid: int | None = None,
    *,
    resolve_unreadable: Callable[..., CwdScan] = resolve_cwds_as_session_user,
) -> str:
    """KILL-SAFETY gate 0's verdict alone — `scan_cwd_processes(...).status`, unchanged in contract.

    Kill-safety callers want only the tri-state and the cheap short-circuit; `reconcile/context.py` calls
    `scan_cwd_processes` for the names."""
    return scan_cwd_processes(
        wt, caller_pid, collect=False, resolve_unreadable=resolve_unreadable,
    ).status


def scan_cwd_processes_multi(
    wts: list[str],
    caller_pid: int | None = None,
    *,
    collect: bool = True,
    resolve_unreadable: Callable[..., list[CwdScan]] = resolve_cwds_as_session_user_multi,
) -> list[CwdScan]:
    """KILL-SAFETY gate 0 for MANY trees on ONE /proc pass: a `CwdScan` per entry of `wts`, positionally.

    Per tree, `scan_cwd_processes`' tri-state and rules, applied once. One /proc walk for all trees (per
    tree it cost 8.8s of a 23-slot pass that a `lease_slot` intent waits behind). A pid counts for EVERY
    tree it sits under (a pool tree nested in the main clone occupies both). `collect=False`
    short-circuits only for a single tree."""
    if not wts:
        return []
    # The kill gates' one-tree short-circuit: return on the first live cwd.
    first_hit_only = not collect and len(wts) == 1
    # No procfs: answers "no occupant", the ALARM degrading open (the module docstring: a probe never
    # gates). Windows explicitly — another process's cwd lives in its PEB there, which this daemon's
    # service account is not granted to read — not by the accident of a missing `\proc`.
    if host_platform.is_windows() or not os.path.isdir("/proc"):
        return [CwdScan("") for _ in wts]
    caller = caller_pid if caller_pid is not None else os.getpid()
    # Whose unreadable cwd means "I cannot see a runner" rather than "not my business". None when there
    # is no switch in play (a dev host, CI, or a process that already IS the session user).
    blind_uid = session_user.session_uid() if session_user.switch_needed() else None
    try:
        wt_reals = [os.path.realpath(wt) for wt in wts]
        ppid_of: dict[int, int] = {}
        for pid in _proc_pids():
            ppid = _proc_ppid(pid)
            if ppid is not None:
                ppid_of[pid] = ppid

        exclude = {caller}
        pid = caller
        while pid in ppid_of and ppid_of[pid] != 1 and ppid_of[pid] not in exclude:
            pid = ppid_of[pid]
            exclude.add(pid)
        children: dict[int, list[int]] = {}
        for p, pp in ppid_of.items():
            children.setdefault(pp, []).append(p)
        queue = [caller]
        while queue:
            for child in children.get(queue.pop(), []):
                if child not in exclude:
                    exclude.add(child)
                    queue.append(child)

        deferred: list[int] = []
        # The verdict is its own flag, never `matched > 0`: a bare `CwdScan("1")` has no records, and a
        # count-derived verdict would open every kill gate.
        per_tree: list[list[dict[str, object]]] = [[] for _ in wt_reals]
        occupied = [False] * len(wt_reals)
        matched = [0] * len(wt_reals)
        for p in ppid_of:
            if p in exclude:
                continue
            # Deny-list BEFORE the cwd read, so a denied pid neither matches nor lands in `deferred`.
            if _is_non_runner_process(p):
                continue
            try:
                target = _proc_cwd(p)
            except OSError as exc:
                # A raced exit (ENOENT) is a skip — it cannot hold a worktree it has left. A permission
                # error is a skip only when the pid is NOT one this node runs sessions as; otherwise the
                # answer is "ask the owner".
                if (blind_uid is not None and exc.errno in (errno.EACCES, errno.EPERM)
                        and blind_uid in _proc_owner_uids(p)):
                    deferred.append(p)
                continue
            # `None` is a MEANINGFUL value here (an unreadable cmdline), so the memo needs its own flag
            # rather than leaning on `is None` — see `_read_cmdline`.
            cmdline: str | None = None
            cmdline_read = False
            for i, wt_real in enumerate(wt_reals):
                if target != wt_real and not target.startswith(wt_real + "/"):
                    continue
                if first_hit_only:
                    return [CwdScan("1")]
                occupied[i] = True
                matched[i] += 1
                if len(per_tree[i]) < _CWD_PROCESS_LIMIT:
                    # An unreadable cmdline is still an occupant, only nameless. Read once per pid.
                    if not cmdline_read:
                        cmdline = _read_cmdline(p)
                        cmdline_read = True
                    per_tree[i].append({
                        "pid": p,
                        "cmd": _cmd_summary(cmdline) if cmdline else UNREADABLE_CMD,
                    })
        local = [
            CwdScan("1" if occupied[i] else "", tuple(per_tree[i]), matched[i])
            for i in range(len(wt_reals))
        ]
        if deferred:
            delegated = resolve_unreadable(wt_reals, deferred, collect=collect)
            return [_merge_scans(local[i], delegated[i]) for i in range(len(wt_reals))]
        return local
    except Exception:  # noqa: BLE001 — a whole-scan crash fails CLOSED (mirrors the bash $_PROBE_FAILED)
        return [CwdScan(PROBE_FAILED) for _ in wts]


def scan_cwd_processes(
    wt: str,
    caller_pid: int | None = None,
    *,
    collect: bool = True,
    resolve_unreadable: Callable[..., CwdScan] = resolve_cwds_as_session_user,
) -> CwdScan:
    """KILL-SAFETY gate 0 — "1" if ANY live process (excluding this call's own process tree) has its cwd
    inside `wt`; "" otherwise; PROBE_FAILED when liveness is UNKNOWN. One /proc pass builds pid->ppid, walks
    UP and DOWN from the caller to exclude, then reads each remaining `/proc/PID/cwd`. The runner launch
    does `cd "$wt" && exec claude`, so a live runner's whole process tree has cwd==wt. `collect=True` also
    names the matches (`CwdScan`, bounded and redacted); `collect=False` keeps the first-hit early return.

    UNREADABLE IS NOT ABSENT, AND THE PID'S OWNER DECIDES WHICH IT IS:
      * ENOENT and friends — the process exited under the scan — stay a SKIP;
      * EACCES/EPERM on a pid owned by the SESSION USER is deferred to `resolve_unreadable` (injectable
        for tests), and any residue is PROBE_FAILED. Skipping it instead, as a raced exit is skipped,
        makes this gate answer "" for a worktree with a live runner in it;
      * EACCES/EPERM on any OTHER uid stays a SKIP — a real host is full of processes this daemon may not
        read and could not have launched as runners. Excludes the daemon's OWN uid, whose cwds are readable.

    `_is_non_runner_process` runs BEFORE either cwd read. The one-tree form of
    `scan_cwd_processes_multi`, which implements all of the above."""
    def delegate(wt_reals: list[str], pids: list[int], *, collect: bool) -> list[CwdScan]:
        return [resolve_unreadable(wt_reals[0], pids, collect=collect)]

    return scan_cwd_processes_multi(
        [wt], caller_pid, collect=collect, resolve_unreadable=delegate,
    )[0]
