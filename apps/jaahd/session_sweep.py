"""The node's half of session transcript capture: find the bytes, redact them, upload them, report.

One tick = one `sweep_once()`: (1) DRAIN — upload every owed session's main, subagent and prompt members
(one gzipped object each via a CP-issued presigned PUT) and report; (2) RECONCILE — tell the CP which
session ids this node's project dirs hold, since both CP-side create signals can drop rows silently;
(3) PROBE — decide whether sessions the CP holds `open` are actually gone, and post a live one's usage so
far (throttled) so its task is priced before it ends; (4) PANE LOG — post the raw pane-log tail of each
unexplained end the CP asks for, so the console can show how a dead run's terminal looked.

A one-shot subprocess (`main.build_scheduled_tasks`): no in-memory state across ticks (the
horizon and the live-usage throttle are files) and outside `AgentsProbeCache` (at most one agents-probe
fork per account per tick). The node NEVER proposes an object key: it names a MEMBER and the CP presigns
(`apps/api/app/session_transcripts.py`). The original transcript is never opened for write: redaction
streams into a scratch copy. Stdlib only (no `boto3`).
"""
from __future__ import annotations

import asyncio
import errno as errno_mod
import gzip
import hashlib
import http.client
import importlib.util
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import agent_providers
import control_plane_api
import env_helpers
import host_platform
from jaah_env import env_get
from reconcile import accounts_probe
import runner_registry
import session_vendor
import transcript_redact
from worktree_mechanics import processes

log = logging.getLogger("jaahd.session_sweep")

# ── `break_cause.munge` — PATH-LOADED, because `apps/jaahd/hooks/` has no `__init__.py` ──────────
# THE ONLY CORRECT MUNGE for a jaah path: the default CLI replaces every NON-ALPHANUMERIC byte with `-`,
# so `/home/jaah/.jaah/repos/<id>/6` becomes `-home-jaah--jaah-repos-<id>-6` (note the
# double hyphen). Never re-spell the rule.
_HOOKS_DIR = Path(__file__).resolve().parent / "hooks"
_BREAK_CAUSE_SPEC = importlib.util.spec_from_file_location(
    "break_cause", _HOOKS_DIR / "break_cause.py"
)
break_cause = importlib.util.module_from_spec(_BREAK_CAUSE_SPEC)
_BREAK_CAUSE_SPEC.loader.exec_module(break_cause)

# ── The wire vocabulary, RE-SPELLED because `apps/jaahd` imports nothing from `apps/api` ─────────
# Mirrors the CP SSOT (`app/session_transcripts.py`, `app/aws/presign.py`); a divergence is a WIRE BREAK,
# which the CP refuses loudly with a 422 naming the field.
MAIN_TRANSCRIPT_MEMBER = "main.jsonl.gz"
# Mirrored so a non-conforming agent id is skipped HERE: `member_keys` is all-or-nothing, so one bad stem
# would cost the session every transcript.
_SUBAGENT_MEMBER = re.compile(r"\Asubagent-[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.jsonl\.gz\Z")
# A vendor's other session files (`agents/base.py::AgentAdapter.session_members`), same mirroring rule.
_VENDOR_MEMBER = re.compile(
    r"\Avendor/(?:[A-Za-z0-9][A-Za-z0-9._-]*/){0,6}[A-Za-z0-9][A-Za-z0-9._-]*\.gz\Z"
)
_VENDOR_MEMBER_MAX = 255  # `MEMBER_NAME_MAX`, mirrored

# The five differently-priced token classes, the `usage_by_model` wire vocabulary; single-homed in
# `apps/api/app/model_prices.py` (`TOKEN_CLASSES`). A rename is a WIRE BREAK (stored `Task.metrics`).
_TOKEN_CLASSES = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# The default provider's Haiku bills a request whose prompt (input plus cache writes and reads) exceeds 100K
# tokens at its long-context tier, suffixed `@long-context` like a vendor's (`_feed_vendor`). The CP derives
# that tier's rate (`apps/api/app/model_prices.py::prices_by_model`). The family is read as the CP's
# `canonical_model` reads it (the adapter's `MODEL_FAMILY_PATTERN`), so both sides agree which model is Haiku.
_HAIKU_LONG_CONTEXT_PROMPT = 100_000


def _is_haiku(model: str) -> bool:
    from agents.claude import MODEL_FAMILY_PATTERN  # noqa: PLC0415 — in-function, as `_vendor_transcript`

    families = re.findall(MODEL_FAMILY_PATTERN, model.lower())
    return bool(families) and families[-1] == "haiku"


# Tools whose input names a concrete target for the re-exploration benchmark; never Bash (free text).
_EXPLORED_TARGET_TOOLS = ("Read", "Edit", "Write", "NotebookEdit", "Grep", "Glob")

# Defensive cap on distinct explored targets per session (JSONB payload size).
_MAX_EXPLORED_TARGETS = 500

# A WIRE BOUND: the ingest route (`MAX_EXPLORED_TARGET_LEN`, `apps/api/app/task_metrics_ingest.py`) 422s the
# WHOLE capture on a longer element (e.g. a free-text Grep `pattern`), so truncate here. Must never EXCEED
# the route's.
_MAX_EXPLORED_TARGET_LEN = 1000

# Wire bounds the ingest side enforces (`apps/api/app/task_metrics_ingest.py`) by refusing the whole
# report — which strands the session `uploading` until `gave_up`, so a runner could delete its own audit
# transcript with 51 fake model names. CLAMPED, not refused: real values sit orders of magnitude below.
# `totals()` derives from `usage_by_model()`'s output so the class-sum invariant survives the clamp.
_MAX_USAGE_MODELS = 50
_MAX_MODEL_NAME = 200
_MAX_TOKEN_COUNT = 10 ** 15

# The `_meta` artifacts, keyed by the suffix `fleet_dispatch.launch_runner` writes them under.
# ORDERED, so a session's members are always named in the same order.
_ARTIFACT_MEMBERS: Tuple[Tuple[str, str], ...] = (
    (".sys", "system-prompt.md.gz"),
    (".task", "task-prompt.md.gz"),
    (".settings.json", "settings.json.gz"),
)
MEMBER_LIMIT = 256
MAX_MEMBER_BYTES = 100 * 1024 * 1024
# Pre-gzip read ceiling, separate from the gzipped PUT ceiling: JSONL gzips 10-15x, and `ReportMemberIn`
# bounds `bytes_raw` at 100 MB, so an oversized raw member would PUT fine then 422 the report five times
# over. Checked BEFORE any read.
MAX_SOURCE_BYTES = MAX_MEMBER_BYTES
# Largest slice handed to `transcript_redact`: its sequential `subn`s make one huge line a ~10x memory
# amplification.
MAX_REDACT_CHUNK = 4 * 1024 * 1024
# Whole scratch budget for ONE session (per-member bounds alone allow 256 x 100 MB).
MAX_SCRATCH_BYTES = 512 * 1024 * 1024
OBSERVED_BATCH = 200
OBSERVED_HORIZON = timedelta(days=90)
CLOCK_SKEW_ALLOWANCE = timedelta(minutes=5)
# Mirrors `app/session_transcripts.py::_SESSION_ID`; filtered here because one bad id 422s a whole
# `observed` batch.
_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
# The one CP-supplied value that reaches a filesystem read whose bytes go to S3, so validated here.
_DISPATCH_REF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# Mirrors `app/node_clone.py::_WINDOWS_ABSOLUTE` (this tree cannot import `app/`).
_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")


def is_a_plain_absolute_path(value: str) -> bool:
    """Mirrors `session_transcripts.is_a_plain_absolute_path` — the THIRD of the CP's three
    `ObservedItem` guards: a `HOME`-less one-shot yields a relative `~/<config home>`, and a DB `config_dir`
    arrives unnormalised; either would 422 the whole batch.
    """
    # Absolute on SOME node OS, and no `..` behind either separator: the CP's rule is
    # `node_clone.is_absolute_node_path` + `node_path_segments` (a Windows node reports `C:\...`).
    if not (value.startswith("/") or _WINDOWS_ABSOLUTE.match(value)):
        return False
    if ".." in value.replace("\\", "/").split("/"):
        return False
    return not any(character < " " or character == "\x7f" for character in value)

# `app/aws/presign.py::FORBIDDEN_HEADERS`: a presigned URL signs only `host`, so any `Content-Type` or
# `x-amz-*` is an unexplained 403. Enforced, not merely obeyed.
FORBIDDEN_HEADERS = frozenset({"content-type"})
FORBIDDEN_HEADER_PREFIX = "x-amz-"

# Quiescence before an `open` session absent from the agents probe (`LiveSessions`) is `abandoned`. Both halves are
# required: a live session can be silent for minutes, and a probe that could not run names nothing.
QUIESCENT_SECONDS = 30 * 60
END_REASON_ABANDONED = "abandoned"

# The live leg's floor between two usage posts for one RUNNING session (`post_live_usage`). The board may
# lag this much; what it buys is the cost bound, since each post re-reads the session's transcripts whole.
LIVE_USAGE_INTERVAL = 180

# The PUT bound, enforced twice: as the socket timeout (per operation, so a slow-drip peer could stretch a
# transfer for hours) and as a transfer DEADLINE in `_ExactlyNBytes`. The runner-facing `artifact.upload`
# budget (`agent_vocabulary.py`, 400s) derives from it; an overrun makes the model retry into a duplicate
# artifact. 300s: a member may be tens of megabytes over a NAT gateway, inside the CP's 15-minute lease.
PUT_TIMEOUT = 300.0

# The upload outcome vocabulary `POST /api/agent/sessions/{sid}/transcript` accepts.
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"
STATUS_UNAVAILABLE = "unavailable"
# Never had a transcript (`app/agent_sessions.py::TRANSCRIPT_NOT_APPLICABLE`): an event-stream run that stored no event.
STATUS_NOT_APPLICABLE = "not_applicable"
#: `sessions.runtime` of a run whose event stream reached the CP (`app/agent_sessions.py::RUNTIME_ACP`).
RUNTIME_ACP = "acp"


# Scratch dir prefix, so `reap_orphan_scratch` touches only its own leavings.
_SCRATCH_PREFIX = "jaah-transcript-"


def _scratch_root(override: Optional[Path] = None) -> Path:
    """Where gzipped members are staged. **NOT `/tmp`, which is tmpfs on this fleet.**

    Every member is gzipped to scratch before the ticket, so the peak is the whole session — on tmpfs
    that is RAM. `state_dir` is real disk.
    """
    root = override if override is not None else env_helpers.state_dir("sessions", "scratch")
    root.mkdir(parents=True, exist_ok=True)
    return root


def reap_orphan_scratch(root: Optional[Path] = None, *, older_than: float = 3600.0,
                        now: Optional[float] = None) -> int:
    """Delete scratch directories a previous tick did not get to clean up. Returns how many.

    A `finally` does NOT run when the 1800s `ScheduledTask` timeout kills the process, so leavings
    would accumulate. Called once at the top of a tick.
    """
    stamp = now if now is not None else time.time()
    reaped = 0
    try:
        with os.scandir(_scratch_root(root)) as scan:
            entries = list(scan)
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(_SCRATCH_PREFIX) or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            if (stamp - entry.stat().st_mtime) <= older_than:
                continue
        except OSError:
            continue
        # BY ABSOLUTE PATH, and only a directory carrying this module's own prefix.
        shutil.rmtree(entry.path, ignore_errors=True)
        reaped += 1
    if reaped:
        log.info("session_sweep: reaped %d orphaned scratch directory/ies from a killed tick", reaped)
    return reaped


class MemberUnreadable(Exception):
    """A member that was on the work list and could not be read.

    `errno` decides whether that is terminal: `unavailable` means the bytes are GONE, but EACCES (the
    readability converge has not reached the file — `env_helpers.share_home_readable`'s 60s floor against
    this sweep's 60s tick) and ENOSPC clear on their own. `None` is the pre-read
    size refusal, reported `failed` like the post-gzip ceiling. Mapping: `_upload_prepared`.
    """

    def __init__(self, message: str, errno: Optional[int] = None) -> None:
        super().__init__(message)
        self.errno = errno


# The errnos that mean GONE; everything else (EACCES, ENOSPC, EIO, ESTALE) can clear, so is retryable.
# `getattr` so a missing name cannot break the module at import.
_ERRNO_GONE = frozenset(
    code for code in (getattr(errno_mod, name, None) for name in ("ENOENT", "ENOTDIR"))
    if code is not None
)


class UploadFailed(Exception):
    """A transport failure, described WITHOUT the URL. **The only thing `put_object` raises outward.**

    `http.client`'s `InvalidURL` carries the full request target, i.e. a live signature, so the boundary
    must convert to this type before anything reaches the reported `error`.
    """


# ── The header rule ───────────────────────────────────────────────────────────────────────────────

def forbidden_headers(names: Iterable[str]) -> Tuple[str, ...]:
    """The names that must NOT accompany a presigned request. Case-insensitive, as headers are."""
    offenders = []
    for name in names:
        folded = name.strip().lower()
        if folded in FORBIDDEN_HEADERS or folded.startswith(FORBIDDEN_HEADER_PREFIX):
            offenders.append(name)
    return tuple(offenders)


def reject_forbidden_headers(names: Iterable[str]) -> None:
    """Raise naming every offending header. NAMES ONLY — a header value may be a session token."""
    offenders = forbidden_headers(names)
    if offenders:
        raise ValueError(
            "a presigned request signs only `host`, so it must not send: " + ", ".join(sorted(offenders))
        )


class _ExactlyNBytes:
    """A read-only view of an open file that yields EXACTLY the declared length to `http.client`.

    `http.client` reads a body until falsy and never consults `Content-Length`, and a runner-owned file
    can change size. GROWN: stop at the vetted length (S3 would only refuse at the end). SHRUNK: raise
    at once rather than hold the connection (and an `_MAX_INFLIGHT` slot) until the timeout.

    Also where `PUT_TIMEOUT` becomes a transfer DEADLINE (one `monotonic()` per chunk). Never buffers:
    one chunk at a time.
    """

    __slots__ = ("_handle", "_declared", "_remaining", "_deadline")

    def __init__(self, handle, declared: int, *, deadline: float | None = None) -> None:
        self._handle = handle
        self._declared = declared
        self._remaining = declared
        self._deadline = deadline

    def grew(self) -> bool:
        """Whether the source has MORE bytes than it declared — asked after the body is sent.

        A grown file would otherwise be silent truncation (S3 answers 200 for the declared bytes). Asked
        afterwards, not raised: a peek before the send would move the body's offset."""
        if self._remaining > 0:
            return False
        return bool(self._handle.read(1))

    def read(self, amount: int = -1) -> bytes:
        if self._remaining <= 0:
            # EOF at the declared length: `http.client` stops on the first falsy answer.
            return b""
        if self._deadline is not None and time.monotonic() > self._deadline:
            # NUMBERS ONLY (see below).
            raise UploadFailed(
                "this PUT passed its %.0fs deadline with %d of %d bytes still to send — a per-socket "
                "timeout cannot bound a transfer that keeps making slow progress, and overrunning the "
                "caller's budget makes it retry into a duplicate"
                % (PUT_TIMEOUT, self._remaining, self._declared)
            )
        want = self._remaining if amount is None or amount < 0 else min(amount, self._remaining)
        if want <= 0:
            # `read(0)` is a legal request for nothing, not a short source.
            return b""
        chunk = self._handle.read(want)
        if not chunk:
            # NUMBERS ONLY: this reaches an operator's page, where a path leaks `$JAAH_HOME`/the account
            # slug and a URL is a live bearer credential.
            raise UploadFailed(
                "the source ran out %d bytes short of the %d declared for this PUT — it shrank after "
                "its size was vetted, and a short body would hold the connection open until the "
                "timeout" % (self._remaining, self._declared)
            )
        self._remaining -= len(chunk)
        return chunk


def _upload_source(source: "Path | int"):
    """The open file `_put_object` streams from: a DUP of an already-vetted descriptor, or a plain open.

    An `int` is a descriptor `cred_broker._open_confined_upload` already vetted via `fstat` (regular,
    runner-owned, single link, confined); `os.dup` keeps those proofs — same inode, no path re-walked.
    The duplicate is the caller's to close. `lseek` to 0 because a dup SHARES the offset — which also
    means reads here move the ORIGINAL descriptor's offset; safe only because `cred_broker` never reads
    it again.
    """
    if isinstance(source, int):
        duplicate = os.dup(source)
        try:
            os.lseek(duplicate, 0, os.SEEK_SET)
            return os.fdopen(duplicate, "rb")
        except BaseException:
            # A leaked fd per failed upload would let a peer exhaust the daemon's descriptors.
            os.close(duplicate)
            raise
    return source.open("rb")


def put_object(url: str, source: "Path | int", *, size: int, timeout: float = PUT_TIMEOUT) -> None:
    """PUT `size` bytes of one file to a presigned URL. **`http.client`, NOT `urllib.request`.**

    Two callers (this sweep's members, `cred_broker`'s runner artifacts by open descriptor), one uploader.
    Not `urllib`: it forces a `Content-Type` onto any body, which a presigned PUT refuses. Streamed with
    an explicit `Content-Length` (under-1 GB-RAM nodes; `UNSIGNED-PAYLOAD` refuses chunked encoding).

    `size` is the caller's already-vetted number and REQUIRED: re-measuring here opened a window between
    the vetted and the sent length. `_ExactlyNBytes` makes it bind. The URL is a bearer credential and is
    NEVER logged or raised.

    The host is NOT pinned (accepted risk): the bucket endpoint is CP configuration this daemon cannot
    know. The CP is the dispatch trust root, and this request carries no credential of ours, so a
    malicious ticket could misdirect bytes but not borrow this node's identity.
    """
    try:
        _put_object(url, source, size=size, timeout=timeout)
    except UploadFailed:
        raise
    except Exception as exc:  # noqa: BLE001 — every transport failure becomes ONE scrubbed outcome
        # `from None`: `__cause__` would carry the original (URL-bearing) message into tracebacks.
        raise UploadFailed("%s during an object PUT" % type(exc).__name__) from None


def _put_object(url: str, source: "Path | int", *, size: int, timeout: float = PUT_TIMEOUT) -> None:
    """`put_object`'s body. Separated ONLY so the wrapper above can be the single scrubbing point."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
    elif parts.scheme == "http":
        # For a loopback test stub only. A real presigned URL is always https.
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    else:
        raise ValueError("an upload URL must be http or https")
    target = parts.path + (("?" + parts.query) if parts.query else "")
    # The caller's number, never a fresh `stat` (see `put_object`).
    headers = {"Content-Length": str(size)}
    # Guards only the dict built above (a future edit adding a header); `http.client`'s own additions are
    # not seen here. Using `http.client` at all is what guarantees no `Content-Type`.
    reject_forbidden_headers(headers.keys())
    try:
        with _upload_source(source) as handle:
            # The deadline starts here; the `connect` above is bounded by the socket timeout.
            body = _ExactlyNBytes(handle, size, deadline=time.monotonic() + timeout)
            connection.request("PUT", target, body=body, headers=headers)
            response = connection.getresponse()
            response.read()
            # Only on success: a grown source was silently truncated to the declared length while S3
            # answered 200.
            if 200 <= response.status < 300 and body.grew():
                raise UploadFailed(
                    "the source is LARGER than the %d bytes declared for this PUT — it grew after its "
                    "size was vetted, so the object stored is that file truncated, and recording it as "
                    "complete would be worse than refusing it" % size
                )
    finally:
        connection.close()
    if not 200 <= response.status < 300:
        # 2xx or nothing: redirects are not followed, so a wrong-region `301` stored NOTHING. Status only.
        raise UploadFailed("S3 answered HTTP %s for an object PUT" % response.status)


# ── The horizon: what keeps this a sweep and not a backfill ───────────────────────────────────────

def horizon_path() -> Path:
    """`<state_dir>/sessions/horizon.ts` — a timestamp persisted as one file under the state dir.

    Must be DURABLE (not the log dir, not tmpfs): a reset floor would blind the reconciler to the
    window between the last boot and this one.
    """
    return env_helpers.state_dir("sessions", "horizon.ts")


def horizon_floor(state_file: Optional[Path] = None, now: Optional[float] = None) -> float:
    """The epoch second at or after which a transcript's mtime makes its session REPORTABLE.

    Keeps this a sweep, not a backfill (unbounded, the first tick would take every historical
    transcript). Written on first run, so the floor is "when this node started reconciling".

    `JAAH_SESSION_HORIZON` overrides it (epoch seconds or ISO-8601); a malformed value is IGNORED with a
    warning, never read as 0.
    """
    override = (env_get("JAAH_SESSION_HORIZON") or "").strip()
    if override:
        parsed = _parse_epoch(override)
        if parsed is not None:
            return parsed
        log.warning(
            "session_sweep: JAAH_SESSION_HORIZON=%r is neither epoch seconds nor an ISO-8601 "
            "timestamp — ignoring it and using the persisted floor", override,
        )
    path = state_file or horizon_path()
    try:
        recorded = _parse_epoch(path.read_text().strip())
        if recorded is not None:
            return recorded
        log.warning("session_sweep: %s does not hold a timestamp — rewriting it", path)
    except OSError:
        pass
    floor = now if now is not None else time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("%.3f\n" % floor)
    except OSError as exc:
        # Still bounds THIS tick, but an unwritable state dir advances the floor every tick, losing
        # sessions that ended in between.
        log.warning("session_sweep: could not persist the horizon floor to %s (%s)", path, exc)
    return floor


def _parse_epoch(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


# ── OD2: whose project directories this node may read at all ──────────────────────────────────────

def slot_project_dirs(worktree_paths: Sequence[str]) -> Dict[str, str]:
    """`{munged project-dir name: the pool-slot path it came from}` — the OD2 fence, MUNGED FORWARD.

    NEVER UN-MUNGE: the transform is lossy, so a guess would attribute a session to another tree. Each of
    this node's own slot paths is munged forward and matched exactly; anything else (the main clone, a
    stray default-CLI run, other nodes' trees) is skipped.
    """
    return {break_cause.project_name(path): path for path in worktree_paths if path}


def _pool_worktree_paths(node_id: str) -> Optional[List[str]]:
    """This node's pool-slot worktree paths, or None on a transport failure.

    None is NOT `[]`: an unknown fence would read as a quiet node, so the caller skips the reconcile leg.
    """
    records = control_plane_api.agent_api_pool_worktrees(node_id)
    if records is None:
        return None
    return [str(record.get("path") or "") for record in records if record.get("path")]


# Margin past `OBSERVED_HORIZON` before `session_vendor.prune` takes a vendor-transcript note: the drain
# can take several ticks after a session becomes reportable.
_PRUNE_MARGIN_SECONDS = 7 * 24 * 3600


# ── Path resolution: get this right or transcripts are silently never found ───────────────────────

def contained_file(path: Path, root: Path) -> Optional[Path]:
    """`path` resolved, but ONLY if it is a real regular file inside `root`. None otherwise.

    Load-bearing: `pathlib`/`scandir` follow symlinks, so a runner-planted link (e.g. to
    `/etc/jaah/node.env`, which user `jaah` may never read) would be uploaded to a one-way,
    kept-forever bucket. The OD2 fence picks the directory; this constrains what a name may point at.

    NOT closed here: `resolve()` also applies to the ROOT, so a root whose last component is a symlink
    makes everything "contained". The vendor leg checks its own root (`_vendor_transcript`); the default
    layout's root is a named follow-up.
    """
    try:
        resolved = path.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError:
        return None
    if root_resolved != resolved.parent and root_resolved not in resolved.parents:
        log.warning(
            # `%r`: a runner names the leaf, and a raw newline would forge a journal record.
            "session_sweep: %r resolves outside %r — refusing to read it",
            str(path), str(root_resolved),
        )
        return None
    if path.is_symlink() or not resolved.is_file():
        log.warning("session_sweep: %r is not a regular file in place — refusing to read it",
                    str(path))
        return None
    return resolved


def open_no_follow(path: Path):
    """`open(path, "rb")` that refuses a symlink AT THE SYSCALL, closing `contained_file`'s TOCTOU gap.

    Check and read are two moments, so a transcript swapped for a symlink after reporting would be
    drained; `O_NOFOLLOW` fails the open instead. It also refuses a FIFO (`O_NONBLOCK` + `S_ISREG`): a
    runner looping `mkfifo` on its transcript path would otherwise hang the one-shot until SIGKILL every
    tick, uploading nothing for any tenant (as in `base.open_regular`).

    No change for real transcripts: `O_NONBLOCK` does nothing to a regular-file open and blocking is
    restored before reading.
    """
    windows = not hasattr(os, "O_NOFOLLOW")
    if windows:
        # Windows has neither flag (the sweep died on `os.O_NOFOLLOW` on a real PC): a reparse
        # point is refused by `lstat` first, and the opened file must be the one `lstat` saw — a swap to a
        # symlink between the two (possible where Developer Mode lets a runner create one) is refused rather
        # than followed. NTFS has no FIFO to block on.
        info = os.lstat(str(path))
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno_mod.ELOOP, "a reparse point, not the transcript", str(path))
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(fd)
            raise OSError(errno_mod.ELOOP, "swapped between check and open, not the transcript", str(path))
    else:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0))
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            # The errno is the classification, and NEVER `ENOENT` (in `_ERRNO_GONE`): a runner could
            # `mkfifo` over a victim's transcript for one tick and permanently retire its row while the
            # real file sits on disk. A directory keeps `EISDIR`; FIFOs, sockets and devices get `ENXIO`,
            # which stays retryable (bounded by `transcript_attempts`).
            raise OSError(errno_mod.EISDIR if stat.S_ISDIR(mode) else errno_mod.ENXIO,
                          "not a regular file", str(path))
        # Belt and braces: Linux ignores `O_NONBLOCK` for regular-file reads. Cleared only to keep the fd
        # conventional.
        if not windows:
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def main_transcript(config_dir: str, worktree_path: str, session_id: str) -> Path:
    """`<config_dir>/projects/<munge(worktree_path)>/<session_id>.jsonl`.

    Composed through `break_cause.project_dir`, which is the single home for that join.
    """
    return Path(break_cause.project_dir(worktree_path, config_dir)) / (session_id + ".jsonl")


def resolve_capture(
    session_id: str,
    worktree_path: str,
    config_dir_hint: Optional[str],
    config_dirs: Sequence[str],
) -> Optional[Tuple[Path, str, object]]:
    """`(transcript path, its config dir, the per-vendor line reader the fold needs)`, or None.

    Called by the drain leg (`upload_session`) and the probe leg (`sweep_once` step 3). Resolving a
    vendor session PINS the vendor id it found (`_vendor_transcript`); the probe leg is the earliest pin.
    A miss falls through to the per-vendor leg, bounded by the session's note, not the roster.

    The third element is `None` for the default layout, else one adapter's `read_usage`. Carried, never
    re-derived: the two folds differ (the default dedups per `message.id`, vendors sum per line), and guessing
    wrong gives plausible numbers off by up to 2.37x.
    """
    if not worktree_path or not session_id:
        return None
    # Validated at the receiver too: an absolute `session_id` discards the join's left side and `../`
    # escapes the fence.
    if not _SESSION_ID.match(session_id):
        log.warning(
            "session_sweep: refusing a session id that cannot be a path segment: %r", session_id
        )
        return None
    candidates: List[str] = []
    if config_dir_hint:
        candidates.append(config_dir_hint)
    candidates.extend(config_dirs)
    seen = set()
    for config_dir in candidates:
        if not config_dir or config_dir in seen or not is_a_plain_absolute_path(config_dir):
            continue
        seen.add(config_dir)
        root = Path(break_cause.project_dir(worktree_path, config_dir))
        found = contained_file(main_transcript(config_dir, worktree_path, session_id), root)
        if found is not None:
            return found, config_dir, None
    # Only when the default layout holds nothing, so its ingest is unchanged. `candidates` IS the fence
    # (see `_vendor_transcript`).
    return _vendor_transcript(session_id, worktree_path, candidates)


def claude_cli_line_tier(session_id: str):
    """The per-line pricing-tier callback for a default-layout session, or None (the default fold).

    Only a provider whose row prices a line by when it ran (`Capabilities.usage.priced_by_time`) carries one
    (its adapter's `line_pricing_tier`); the dispatch note (`session_vendor`) says which provider ran
    the session. Never raises: an unreadable note costs the tier, logged, never the capture."""
    try:
        note = session_vendor.read(session_id) or {}
        provider = str(note.get("provider") or "")
        caps = agent_providers.capabilities_of(provider)
        if caps is None or not caps.usage.priced_by_time:
            return None
        import agents  # in-function, as `_vendor_transcript`: keeps the one-shot's start-up light

        return getattr(agents.get_adapter(provider), "line_pricing_tier", None)
    except Exception as exc:  # noqa: BLE001 — a missing tier under-bills one session; a raise loses it
        log.warning("session_sweep: session %s's pricing tier could not be resolved (%s: %r) — its usage "
                    "is folded at the base rate", session_id, type(exc).__name__, exc)
        return None


class VendorLookupFailed(Exception):
    """The per-vendor lookup could not be COMPLETED — as distinct from completing and finding nothing.

    A retry vs a deletion: `unavailable` is TERMINAL, right for bytes that are gone but wrong for
    `EMFILE`, an EFS stall or the convergence `EACCES` window, which clear within a tick (the same line
    `_ERRNO_GONE` draws for the main read). Raised only for a lookup that FAILED.
    """


#: How far a line's own `timestamp` may sit past a window edge and still count as inside it. The edges are the
#: node's clock (`session_vendor.record` / `close_usage_window`), the stamps the CLI's: one clock on a static slot
#: (the only place a hand-off runs today), a guest's own in a MicroVM, where kvm-clock or an NTP step leaves it a
#: fraction of a second off. Kept below the gap that separates the two fronts' lines at either edge — the ceiling
#: is stamped once the old front is dead, the floor at the new front's launch, after the old one closed and was
#: killed — so the slack absorbs skew without counting another front's line twice.
USAGE_WINDOW_SKEW_S = 2.0


class _WindowedReader:
    """`read_usage` folding only the spend stamped inside `[floor, ceiling)` (0.0 = that side open), each edge
    widened by `USAGE_WINDOW_SKEW_S` — a row whose vendor transcript a console hand-off shares with another front.

    A resumed row's transcript also holds the earlier front's turns (before its `usage_floor`), and a row that
    handed its conversation on sees the next front's turns appended after its `usage_ceiling`; both are counted
    on those fronts' own rows, so summing them here would count them twice. A spending line outside the window
    reads as zero spend that still names its model (the model carries forward) and is counted in `dropped`,
    which the fold's summary reports; a spending line with no parseable top-level `timestamp` raises, so the
    fold marks the usage incomplete rather than counting it twice or dropping it silently. Every hand-off CLI's
    transcript stamps each record with one.
    """

    def __init__(self, read_usage, floor: float, ceiling: float) -> None:
        self._read_usage = read_usage
        self._floor = floor - USAGE_WINDOW_SKEW_S if floor else 0.0
        self._ceiling = ceiling + USAGE_WINDOW_SKEW_S if ceiling else 0.0
        self.dropped = 0

    def __call__(self, raw: bytes):
        counts = self._read_usage(raw)
        if counts is None or not any((counts.input, counts.output, counts.cache_read,
                                      counts.cache_write_5m, counts.cache_write_1h)):
            return counts
        try:
            record = json.loads(raw)
        except ValueError:
            record = None
        stamp = record.get("timestamp") if isinstance(record, dict) else None
        epoch = _parse_epoch(stamp) if isinstance(stamp, str) else None
        if epoch is None:
            raise ValueError("a spending line under a usage window carries no parseable timestamp")
        if (self._floor and epoch < self._floor) or (self._ceiling and epoch >= self._ceiling):
            from agents.base import TokenCounts  # noqa: PLC0415 — in-function, as `_vendor_transcript`
            self.dropped += 1
            return TokenCounts(model=counts.model)
        return counts


def _vendor_transcript(
    session_id: str, worktree_path: str, candidates: Sequence[str]
) -> Optional[Tuple[Path, str, object]]:
    """`(transcript path, its config dir, that vendor's line reader)` for a session whose CLI mints its
    own id, else None.

    A CLI that mints its own id writes where the roster scan cannot compose; the node's dispatch note
    (`session_vendor`) plus the adapter bridge it. Only a `SESSION_DISCOVER` adapter reaches this (else a
    default-layout transcript could be summed per line, inflating 2.37x).

    Two fences: the home must be among the CP-named `candidates` AND match the note's recorded account —
    else a rotated-away session resolves to another account's live transcript. The vendor id is PINNED
    once resolved (first-wins), since discovery has a floor but no ceiling. Remaining gap: a re-leased
    slot whose successor writes before this session's first drain (needs a CP field; deferred).
    Contained like the default path, against the vendor's transcript subtree.

    `None` = the lookup completed and found nothing (reported terminally, truthfully);
    `VendorLookupFailed` = it could not complete (retryable). Nothing else escapes.
    """
    note = session_vendor.read(session_id)
    if not note:
        return None
    provider = str(note.get("provider") or "")
    if not provider:
        return None
    # In-function to keep the one-shot's start-up free of the adapter package (not a cycle). The import and
    # registry lookup are inside the fence: a broken adapter raises real module-level errors here, and
    # must cost one session, not the tick.
    try:
        import agents
    except Exception as exc:  # noqa: BLE001 — an in-function import runs real module-level code
        log.warning("session_sweep: the adapter package could not be imported (%s: %r)",
                    type(exc).__name__, exc)
        raise VendorLookupFailed("the adapter package could not be imported") from exc
    try:
        adapter = agents.get_adapter(provider)
        binding = adapter.session_binding()
    except agents.UnknownProvider:
        # A completed lookup: no adapter here, and a retry will not change that.
        log.warning("session_sweep: session %s was dispatched as provider %r, which this node has no "
                    "adapter for — its transcript cannot be located", session_id, provider)
        return None
    except Exception as exc:  # noqa: BLE001 — one unusable adapter may not cost the tick
        log.warning("session_sweep: the %s adapter could not be consulted (%s: %r)",
                    provider, type(exc).__name__, exc)
        raise VendorLookupFailed("the %s adapter could not be consulted" % provider) from exc
    if binding != agents.SESSION_DISCOVER:
        return None
    recorded_home = str(note.get("config_dir") or "")
    # Normalized both sides: the note and the candidates are spelled by different code, and a trailing
    # separator would make the vendor leg silently inert. `normpath`, not `realpath`: resolving
    # runner-controlled symlinks would let two homes compare equal — this line IS the fence.
    def _same_dir(candidate: str) -> bool:
        return os.path.normpath(candidate) == os.path.normpath(recorded_home)

    home = next((c for c in candidates
                 if c and is_a_plain_absolute_path(c) and _same_dir(c)), "")
    if not home:
        log.warning("session_sweep: session %s was dispatched on account home %r, which is not among "
                    "the %d the control plane named for it — refusing to look for its transcript "
                    "anywhere else", session_id, recorded_home, len(candidates))
        return None
    pinned = str(note.get("vendor_session_id") or "")
    replaced_dead_pin = False
    since = session_vendor.launch_floor(note)
    try:
        vendor_session = pinned or adapter.discover_session_id(
            config_dir=home, worktree=worktree_path, since=since)
        path = adapter.transcript(
            config_dir=home, worktree=worktree_path, session_id=vendor_session
        ) if vendor_session else None
        # A DEAD PIN is re-opened: otherwise a co-tenant's decoy wins the first-wins pin, is unlinked,
        # and every later drain reports terminal `unavailable` over a real transcript. Re-discovering when
        # the pinned file is absent costs the drift protection nothing.
        if pinned and not path:
            rediscovered = adapter.discover_session_id(
                config_dir=home, worktree=worktree_path, since=since)
            if rediscovered and rediscovered != pinned:
                log.warning("session_sweep: session %s's pinned vendor session %r is not on disk; "
                            "re-resolving found %r instead — the pin is being replaced",
                            session_id, pinned, rediscovered)
                vendor_session, pinned, replaced_dead_pin = rediscovered, "", True
                path = adapter.transcript(
                    config_dir=home, worktree=worktree_path, session_id=rediscovered)
    except Exception as exc:  # noqa: BLE001 — a vendor reader may not cost the sweep its tick
        # A lookup failure, not `None`: most of what an adapter raises clears within a tick.
        log.warning("session_sweep: locating session %s's transcript through the %s adapter raised "
                    "(%s: %r)", session_id, provider, type(exc).__name__, exc)
        raise VendorLookupFailed("%s adapter lookup raised %s" % (provider, type(exc).__name__)) from exc
    if not path:
        # Logged: the ordinary not-written-yet exit, and also what a vendor-side change would make permanent.
        # INFO, since it fires every tick until the CLI writes.
        log.info("session_sweep: session %s (provider %s) has no transcript under %s yet — %s",
                 session_id, provider, home,
                 "its recorded vendor session is not on disk" if pinned
                 else "nothing on that account is attributable to this run")
        return None
    # Contained against the vendor's transcript SUBTREE, not the account home, which also holds the OAuth
    # credential file — one runner-planted hardlink away from a kept-forever bucket. Only the adapter knows
    # the layout (`base.AgentAdapter.transcript_root`).
    try:
        root = adapter.transcript_root(config_dir=home)
    except Exception as exc:  # noqa: BLE001 — same rule as the lookup above: never cost the tick
        log.warning("session_sweep: the %s adapter could not name its transcript root under %r "
                    "(%s: %r)", provider, home, type(exc).__name__, exc)
        raise VendorLookupFailed(
            "%s adapter could not name its transcript root" % provider) from exc
    # The root may not be a symlink: its parent is `jaah`-writable, and `ln -s /` there would make
    # `contained_file` pass for anything. Checked here, not in `contained_file` (the default ingest path).
    try:
        if stat.S_ISLNK(os.lstat(root).st_mode):
            # Retryable, not terminal: a one-syscall state, and `None` would let `ln -s /` retire a
            # co-tenant's row.
            log.warning("session_sweep: session %s's transcript root %r is a symlink — refusing it, "
                        "because a root that resolves elsewhere contains nothing", session_id, root)
            raise VendorLookupFailed("the %s transcript root is a symlink" % provider)
    except OSError as exc:
        # The `_ERRNO_GONE` split: absent is an honest "no transcript"; any other errno is a failure to
        # LOOK (EACCES, EIO), which is retryable.
        if exc.errno in _ERRNO_GONE:
            return None
        log.warning("session_sweep: session %s's transcript root %r could not be examined (%s: %r)",
                    session_id, root, type(exc).__name__, exc)
        raise VendorLookupFailed("cannot stat the %s transcript root" % provider) from exc
    found = contained_file(Path(path), Path(root))
    if found is None:
        return None
    if not pinned:
        # `replace=` only for a pin proved dead above (see `pin_vendor_session`).
        session_vendor.pin_vendor_session(session_id, vendor_session, replace=replaced_dead_pin)
    floor, ceiling = session_vendor.usage_floor(note), session_vendor.usage_ceiling(note)
    return found, home, (_WindowedReader(adapter.read_usage, floor, ceiling) if floor or ceiling
                         else adapter.read_usage)


def vendor_transcript_path(session_id: str, worktree_path: str, config_dir: str) -> Optional[Path]:
    """`_vendor_transcript`'s path alone, for a live runner the node itself dispatched onto `config_dir` —
    same note, same fences, same pin. None when there is none (yet); `VendorLookupFailed` when the lookup
    could not complete."""
    found = _vendor_transcript(session_id, worktree_path, [config_dir])
    return found[0] if found else None


def vm_transcript_path(provider: str, session_id: str, config_dir: str, worktree: str,
                       since: float) -> Optional[Path]:
    """A MicroVM session's vendor transcript, under the VM's OWN copy of the account (`<vmdir>/config`, which
    holds no other session's files: `microvm_session`'s config share), as the CLI in the guest named it from its
    guest `worktree`. No dispatch note: the VM is the fence a note is on a shared home, and `since` (the launch)
    the floor. A guest writes that tree as the daemon's own uid, so EVERY directory from the share down to the
    adapter's transcript root must be a real one — checked before the adapter reads anything, since a link above
    the root would carry its whole walk into another VM's share — and the answer is contained against that root.
    None when there is none (yet); `VendorLookupFailed` when the lookup could not complete."""
    import agents  # in-function, as `_vendor_transcript`
    try:
        adapter = agents.get_adapter(provider)
        root = Path(adapter.transcript_root(config_dir=config_dir))
    except Exception as exc:  # noqa: BLE001 — a vendor reader may not cost the caller its tick
        raise VendorLookupFailed("%s adapter could not name its transcript root" % provider) from exc
    base = Path(config_dir)
    try:
        for here in [base, *(base / Path(*root.relative_to(base).parts[:n])
                             for n in range(1, len(root.relative_to(base).parts) + 1))]:
            if not stat.S_ISDIR(os.lstat(here).st_mode):  # `lstat`: a symlink is never a directory here
                raise VendorLookupFailed("a link or file sits between %s and the %s transcript root"
                                         % (config_dir, provider))
    except ValueError as exc:
        raise VendorLookupFailed("the %s transcript root is not under %s" % (provider, config_dir)) from exc
    except OSError as exc:
        if exc.errno in _ERRNO_GONE:
            return None  # the CLI has not written its tree yet
        raise VendorLookupFailed("cannot stat the %s transcript root" % provider) from exc
    try:
        vendor = (adapter.discover_session_id(config_dir=config_dir, worktree=worktree, since=since)
                  if adapter.session_binding() == agents.SESSION_DISCOVER else session_id)
        path = adapter.transcript(config_dir=config_dir, worktree=worktree, session_id=vendor) if vendor else None
    except Exception as exc:  # noqa: BLE001 — a vendor reader may not cost the caller its tick
        raise VendorLookupFailed("%s adapter lookup raised %s" % (provider, type(exc).__name__)) from exc
    if not path:
        return None
    found = contained_file(Path(path), root)
    # No link below the root either: `contained_file` resolves both sides, so a swapped directory under the root
    # that points back inside it would pass; the answer must be the path the adapter named, as written.
    try:
        named = root.resolve(strict=True) / Path(os.path.normpath(path)).relative_to(root)
    except (OSError, ValueError):
        return None
    return found if found == named else None


def subagent_members(transcript: Path, session_id: str) -> List[Tuple[str, Path]]:
    """`[(member name, path)]` for `<project dir>/<session_id>/subagents/agent-*.jsonl`. EXACTLY that.

    No other subdirectory is ever read: a `memory/` dir, `<session_id>/tool-results/` and the
    `agent-*.meta.json` sidecars all sit nearby and are not transcripts. A missing `subagents/` is valid
    (zero subagents). `agent-abc.jsonl` becomes `subagent-abc.jsonl.gz`; a stem the CP could not key is
    skipped, since `member_keys` refuses the whole batch on one bad name.
    """
    directory = transcript.parent / session_id / "subagents"
    if not directory.is_dir() or directory.is_symlink():
        return []
    # The default layout only: under a vendor's shared date-bucketed tree a co-tenant (same uid) could plant
    # `<id>/subagents/agent-*.jsonl` and have it folded into the victim's audit record and bill, since
    # containment checks where and what, never who wrote it. `upload_session` gates this to the default leg.
    members: List[Tuple[str, Path]] = []
    for path in sorted(directory.glob("agent-*.jsonl")):
        if contained_file(path, directory) is None:
            continue
        name = "subagent-" + path.stem[len("agent-"):] + ".jsonl.gz"
        if not _SUBAGENT_MEMBER.match(name):
            log.warning(
                "session_sweep: skipping %s — %r is not a member name the control plane issues a key "
                "for, and one unusable name refuses the whole ticket", path, name,
            )
            continue
        members.append((name, path))
    return members


def vendor_session_members(
    session_id: str, transcript: Path, config_dir: str
) -> Tuple[List[Tuple[str, Path]], List[str], Optional[Path]]:
    """`([(member name, path)], [omission notes], the vendor transcript root they are contained in)` — the rest of a non-default vendor's record for this
    session: its sub-agent threads and every other session file (`AgentAdapter.session_members`).

    Without this a non-default capture is its main file alone, and every sub-agent turn, spilled tool
    output and session file the run produced is lost. Each path is contained against the adapter's own
    transcript root (the account home also holds its OAuth credential), each name re-checked against the
    wire allowlist. Anything that cannot be listed or named is an OMISSION NOTE for the manifest, never a
    silent skip and never a lost session: the main file still uploads.

    Attribution is the vendor's own link (a sub-agent rollout's recorded root id, a chat dir named for the
    session), which a runner on the SAME ACCOUNT can forge — the same boundary the main-file lookup already
    rests on (`_vendor_transcript`): the account, not the file. A co-tenant on another account cannot reach
    this tree at all.
    """
    note = session_vendor.read(session_id) or {}
    provider = str(note.get("provider") or "")
    if not provider:
        return [], [], None
    notes: List[str] = []
    try:
        import agents
        adapter = agents.get_adapter(provider)
        root = Path(adapter.transcript_root(config_dir=config_dir))
        # Re-checked here, not trusted from the main-file lookup: a root that became a symlink would make
        # every descriptor check below pass for anything under its target.
        if stat.S_ISLNK(os.lstat(root).st_mode):
            raise VendorLookupFailed("the %s transcript root is a symlink" % provider)
        try:
            listed = adapter.session_members(config_dir=config_dir, transcript=str(transcript))
        except agents.base.IncompleteSessionMembers as partial:
            log.warning("session_sweep: %s — %s", session_id, partial)
            listed = partial.members
            notes.append("%s session files (listing incomplete; %d found)" % (provider, len(listed)))
    except Exception as exc:  # noqa: BLE001 — a side-file walk may cost its own members, never the session
        log.warning("session_sweep: %s — the %s adapter could not list this session's other files "
                    "(%s: %r); the main transcript still uploads", session_id, provider,
                    type(exc).__name__, exc)
        return [], ["%s session files (listing failed: %s)" % (provider, type(exc).__name__)], None
    members: List[Tuple[str, Path]] = []
    dropped: List[str] = notes
    named: set = set()
    unkeyable = 0
    for name, path in listed:
        allowed = bool(_SUBAGENT_MEMBER.match(name)) or (
            len(name) <= _VENDOR_MEMBER_MAX and bool(_VENDOR_MEMBER.match(name)))
        if not allowed:
            unkeyable += 1
            # `ascii()` and a short slice: a runner chooses these names, and the CP measures this field
            # JSON-escaped, where one emoji costs twelve bytes.
            dropped.append("%s (unkeyable name)" % ascii(name)[:80])
            continue
        if name in named:
            # A repeated name 422s the whole ticket, so a second one is never sent.
            dropped.append("%s (named twice)" % name)
            continue
        named.add(name)
        found = contained_file(Path(path), root)
        if found is None:
            dropped.append("%s (outside the vendor transcript root)" % name)
            continue
        if _link_count(found) != 1:
            # A hard link is how a runner on this account would smuggle the account's own credential into
            # a session dir: the vendor never links what it writes, so a second name is a refusal.
            log.warning("session_sweep: %s — %r has more than one link; refusing it", session_id, str(found))
            dropped.append("%s (hard-linked)" % name)
            continue
        members.append((name, found))
    if unkeyable:
        # One line per session, never one per file (a runner chooses how many).
        log.warning("session_sweep: %s — %d session file(s) carry a name the control plane issues no key "
                    "for; skipped and named on the manifest", session_id, unkeyable)
    return members, dropped, root


# Bytes of `members_not_uploaded` in the report: the CP refuses a `redaction` over 8192 bytes JSON-escaped,
# and the rest of that field must still fit beside it.
_NOTES_BUDGET = 4096


def _bounded_notes(notes: Sequence[str]) -> List[str]:
    """At most 20 notes and `_NOTES_BUDGET` JSON-escaped bytes, with a final count of what was cut — so an
    omission list a runner inflated cannot cost the report it rides on."""
    kept: List[str] = []
    for note in notes[:20]:
        if len(json.dumps(kept + [note])) > _NOTES_BUDGET:
            break
        kept.append(note)
    if len(kept) < len(notes):
        kept.append("... and %d more" % (len(notes) - len(kept)))
    return kept


def _link_count(path: Path) -> int:
    """`path`'s hard-link count, or 0 when it cannot be read (which the caller refuses too)."""
    try:
        return os.lstat(path).st_nlink
    except OSError:
        return 0


def artifact_members(worktree_path: str, dispatch_ref: Optional[str]) -> List[Tuple[str, Path]]:
    """The prompts and settings this session ran under — `[(member name, path)]`, **only where they exist**.

    `<dirname(worktree_path)>/_meta/<dispatch_ref>.{sys,task,settings.json}`, where
    `fleet_dispatch.launch_runner` writes them; derived node-side, not sent by the CP. Missing is not a
    failure: an interactive session has none of them.
    """
    if not worktree_path or not dispatch_ref:
        return []
    if not _DISPATCH_REF.match(str(dispatch_ref)):
        # The final segment comes from the CP, so it is checked too.
        log.warning("session_sweep: refusing a dispatch ref that cannot be a path segment: %r",
                    dispatch_ref)
        return []
    meta = Path(os.path.dirname(worktree_path.rstrip("/"))) / "_meta"
    members: List[Tuple[str, Path]] = []
    for suffix, name in _ARTIFACT_MEMBERS:
        found = contained_file(meta / (dispatch_ref + suffix), meta)
        if found is not None:
            members.append((name, found))
    return members


# ── The single pass: redact, hash, gzip, and fold the analytics ───────────────────────────────────

class MemberPrep:
    """One member ready to PUT: the gzipped file, its two sizes, its digest, and what was redacted."""

    __slots__ = ("name", "source", "gz_path", "bytes", "bytes_raw", "sha256", "redaction_counts")

    def __init__(self, name, source, gz_path, size, size_raw, digest, counts):
        self.name = name
        self.source = source
        self.gz_path = gz_path
        self.bytes = size
        self.bytes_raw = size_raw
        self.sha256 = digest
        self.redaction_counts = counts

    def wire(self) -> Dict[str, object]:
        """The `ReportMemberIn` shape: `bytes` is the gzipped object, `bytes_raw` what the tar holds."""
        return {
            "name": self.name,
            "bytes": self.bytes,
            "bytes_raw": self.bytes_raw,
            "sha256": self.sha256,
        }


class Analytics:
    """The token/model/exploration fold, run in the SAME pass that redacts — the transcript is read
    exactly once.

    The single home for the fold because it is the single pass over the transcript; the project-dir
    munge lives in `break_cause`.

    Per RESPONSE, not per line: one response spans several JSONL lines repeating a byte-identical
    `usage`, and a per-line sum measured up to 2.37x too high. `totals()` is DERIVED from
    `usage_by_model()`'s per-class decomposition, so the four input classes sum to `tokens_in` and
    `output` to `tokens_out` by construction — `apps/api/app/task_run_metrics.py` marks a task `partial`
    if they diverge.
    """

    __slots__ = ("_folded", "model", "_lines", "_explored", "_explored_seen", "_read_usage",
                 "_reader_errors", "_line_tier")

    def __init__(self, read_usage=None, line_tier=None) -> None:
        self._folded: Dict[tuple, Tuple[str, Dict[str, int]]] = {}
        self.model: Optional[str] = None
        self._lines = 0
        self._explored: List[str] = []
        self._explored_seen: set = set()
        # `None` reads the transcript in the default layout; otherwise one vendor adapter's `read_usage`
        # (`_feed_vendor`).
        self._read_usage = read_usage
        # Lines the vendor reader refused with a raise; counted, not logged per line (see `_feed_vendor`).
        self._reader_errors = 0
        # The default fold only: `(parsed line) -> pricing-tier suffix`, from an adapter whose runners are
        # the default CLI billed by the line's own time (a `@peak` tier). `None` leaves every key as it was.
        self._line_tier = line_tier

    def feed(self, raw: bytes) -> None:
        if self._read_usage is not None:
            self._feed_vendor(raw)
            return
        self._lines += 1
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(obj, dict):
            return
        message = obj.get("message")
        if not isinstance(message, dict):
            return
        # Before the early returns: a line with no usable `usage` still explored a real target.
        self._scan_targets(message.get("content"))
        if message.get("role") != "assistant":
            return
        # The last assistant message that NAMES a model wins.
        model = message.get("model")
        if model:
            self.model = str(model)
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        # Known gap: `usage.speed` is not read, so fast-mode spend would be priced at standard rates
        # (unreachable via the default CLI today; needs a rate axis in `model_prices`). `_int(x)`, never
        # `.get(k, 0)`: an explicit JSON null must degrade one number, not drop the capture. The residual of
        # `cache_creation_input_tokens` over the two TTL keys goes to 5m (the API's default TTL) so the
        # class sum holds by construction; it is 0 in every observed case.
        created = _int(usage.get("cache_creation_input_tokens"))
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, dict):
            write_5m = _int(cache_creation.get("ephemeral_5m_input_tokens"))
            write_1h = _int(cache_creation.get("ephemeral_1h_input_tokens"))
        else:
            write_5m, write_1h = 0, 0
        write_5m += max(0, created - write_5m - write_1h)
        classes = {
            "input": _int(usage.get("input_tokens")),
            "cache_write_5m": write_5m,
            "cache_write_1h": write_1h,
            "cache_read": _int(usage.get("cache_read_input_tokens")),
            "output": _int(usage.get("output_tokens")),
        }
        # No `model` buckets under "unknown", keeping class sums equal; the CP names unpriced models.
        line_model = str(model or "unknown")
        prompt = classes["input"] + classes["cache_write_5m"] + classes["cache_write_1h"] + classes["cache_read"]
        if prompt > _HAIKU_LONG_CONTEXT_PROMPT and _is_haiku(line_model):
            line_model += "@long-context"
        if self._line_tier is not None:
            # The same suffix mechanism as a vendor's `@long-context` (`_feed_vendor`): one bucket per tier.
            # Guarded like the vendor reader: one line the callback cannot read costs that line its tier.
            try:
                line_model += str(self._line_tier(obj) or "")
            except Exception:  # noqa: BLE001 — a runner-written line may not cost the capture
                self._reader_errors += 1
        # No `message.id`, no dedup: over-counting is questionable, collapsing distinct responses would
        # silently DELETE billed tokens. The line number keys a group of its own.
        message_id = message.get("id")
        key = (message_id, obj.get("requestId")) if message_id is not None else (None, self._lines)
        seen = self._folded.get(key)
        if seen is None:
            self._folded[key] = (line_model, classes)
        else:
            # Per-class maximum, not first-wins, so a zero placeholder line cannot win; the model comes from
            # the line with the most spend.
            seen_model, seen_classes = seen
            self._folded[key] = (
                line_model if sum(classes.values()) > sum(seen_classes.values()) else seen_model,
                {cls: max(seen_classes[cls], classes[cls]) for cls in _TOKEN_CLASSES},
            )

    def _feed_vendor(self, raw: bytes) -> None:
        """Fold one line of a NON-DEFAULT transcript, through the adapter that knows that vendor.

        The adapter answers what one line SPENT in the five classes; this owes the shared arithmetic and
        never lets a reader failure cost the capture. One group per line (no default-style dedup): both
        adapters write usage once per turn, measured. A reader whose transcript also carries a CUMULATIVE
        total must report the per-turn figure. A line may name a model without spending; it carries
        forward (`base.AgentAdapter.read_usage`). No `_scan_targets`: the tool names are the default CLI's.
        """
        self._lines += 1
        try:
            counts = self._read_usage(raw)
        except Exception as exc:  # noqa: BLE001 — one bad line must cost one line, never the capture
            # Logged once per capture: runner-written deeply-nested lines raise `RecursionError` past the
            # adapters' `except`, and ~10,000 per-line warnings would flood the daemon's unbounded stderr
            # buffer. The count rides the summary line.
            self._reader_errors += 1
            if self._reader_errors == 1:
                log.warning("session_sweep: a per-vendor usage reader raised on one line (%s: %r) — "
                            "that line reports nothing, the rest of the transcript is still folded, "
                            "and any further such lines are counted rather than logged",
                            type(exc).__name__, exc)
            return
        if counts is None:
            return
        if counts.model:
            # Clamped: `_upload_prepared` sends `model` verbatim and the CP's `max_length=200` would 422 the
            # whole report after upload (a runner deleting its own audit record). The default leg is left
            # unclamped deliberately (fenced; no default-provider model name nears 200) — a follow-up.
            self.model = str(counts.model)[:_MAX_MODEL_NAME]
        # Clamped at zero for the vendor leg: the CP refuses a negative count after every member is
        # uploaded, costing the capture. The default leg has the same exposure and is left fenced — a
        # follow-up.
        classes = {
            "input": max(0, _int(counts.input)),
            "cache_write_5m": max(0, _int(counts.cache_write_5m)),
            "cache_write_1h": max(0, _int(counts.cache_write_1h)),
            "cache_read": max(0, _int(counts.cache_read)),
            "output": max(0, _int(counts.output)),
        }
        if not any(classes.values()):
            # Zero-spend: `usage_by_model()` would drop the bucket anyway.
            return
        # Keyed by MODEL and summed in place (a per-line key would cost ~2.5M entries at the 100 MB ceiling),
        # and capped at insert: a transcript can name a fresh model every line (~1.1 GB RSS measured).
        # Overflow folds into one bucket rather than being dropped — never understate a bill.
        model = str(counts.model or self.model or "unknown")
        if counts.pricing_tier:
            model += str(counts.pricing_tier)
        model = model[:_MAX_MODEL_NAME]
        key = (None, model)
        previous = self._folded.get(key)
        # The overflow bucket is reserved INSIDE the cap (cap-1 named + 1), so `usage_by_model()` never has
        # to drop its smallest bucket.
        if previous is None and len(self._folded) >= _MAX_USAGE_MODELS - 1:
            key = (None, "unknown")
            previous = self._folded.get(key)
            model = previous[0] if previous is not None else "unknown"
        if previous is not None:
            for cls in _TOKEN_CLASSES:
                classes[cls] += previous[1][cls]
        self._folded[key] = (model, classes)

    def _scan_targets(self, content: object) -> None:
        """Collect the file/dir/pattern argument of every `_EXPLORED_TARGET_TOOLS` call in one message's
        content blocks — deduped, order-preserving, capped."""
        if not isinstance(content, list):
            # A string `content` (a plain user turn) carries no structured tool call at all.
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in _EXPLORED_TARGET_TOOLS:
                continue
            if len(self._explored) >= _MAX_EXPLORED_TARGETS:
                return
            inp = block.get("input")
            if not isinstance(inp, dict):
                continue
            target = inp.get("file_path") or inp.get("path") or inp.get("pattern")
            if not target:
                continue
            # Truncated BEFORE the dedup, so near-duplicates collapse to one accepted entry.
            target = str(target)[:_MAX_EXPLORED_TARGET_LEN]
            if target in self._explored_seen:
                continue
            self._explored_seen.add(target)
            self._explored.append(target)

    @property
    def lines(self) -> int:
        """How many transcript lines were fed (the denominator for `reader_errors`)."""
        return self._lines

    @property
    def window_dropped(self) -> int:
        """How many spending lines a hand-off's usage window (`_WindowedReader`) left to another front's row."""
        return int(getattr(self._read_usage, "dropped", 0))

    @property
    def reader_errors(self) -> int:
        """How many lines the per-vendor usage reader refused with a raise (always 0 for the default fold);
        `_feed_vendor` logs only the first."""
        return self._reader_errors

    def usage_by_model(self) -> Dict[str, Dict[str, int]]:
        """`{model: {input, cache_write_5m, cache_write_1h, cache_read, output}}` — the only priceable
        shape (`tokens_in` conflates five classes billed over a ~40x spread)."""
        by_model: Dict[str, Dict[str, int]] = {}
        for model, classes in self._folded.values():
            bucket = by_model.setdefault(model, {cls: 0 for cls in _TOKEN_CLASSES})
            for cls in _TOKEN_CLASSES:
                bucket[cls] += classes[cls]
        # Zero-spend models are dropped (e.g. the default CLI's `<synthetic>` placeholders), matched on no
        # spend rather than a client-specific name; the class-sum invariant is unaffected.
        spent = {model: classes for model, classes in by_model.items() if any(classes.values())}

        # The three wire clamps (`_MAX_USAGE_MODELS`). Truncate the NAME first so names differing only past
        # the cutoff merge before the count cap.
        clamped: Dict[str, Dict[str, int]] = {}
        for model, classes in spent.items():
            bucket = clamped.setdefault(model[:_MAX_MODEL_NAME], {cls: 0 for cls in _TOKEN_CLASSES})
            for cls in _TOKEN_CLASSES:
                bucket[cls] = min(bucket[cls] + classes[cls], _MAX_TOKEN_COUNT)
        if len(clamped) > _MAX_USAGE_MODELS:
            # Keep the BIGGEST spenders; reachable only by a fabricated blob.
            clamped = dict(
                sorted(clamped.items(), key=lambda kv: -sum(kv[1].values()))[:_MAX_USAGE_MODELS]
            )
        return clamped

    def explored_targets(self) -> List[str]:
        """Every target an exploration tool named, first-seen order (the re-exploration benchmark's input)."""
        return list(self._explored)

    def totals(self) -> Tuple[int, int]:
        """`(tokens_in, tokens_out)`, DERIVED from the same per-class fold `usage_by_model()` reports.

        Derived, so the class-sum invariant holds by construction."""
        tokens_in = 0
        tokens_out = 0
        # Off `usage_by_model()`, not the raw fold, so the invariant survives the clamps
        # (`app/task_run_metrics.py` would otherwise mark every task `partial`).
        for classes in self.usage_by_model().values():
            tokens_in += sum(classes[cls] for cls in _TOKEN_CLASSES if cls != "output")
            tokens_out += classes["output"]
        return tokens_in, tokens_out


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _assert_contained_fd(handle, root: Path, name: str) -> None:
    """Refuse an OPENED vendor member unless the file it is really reading lies under `root`, has one
    link and is within the read ceiling — asserted on the descriptor, not the path.

    `open_no_follow` guards only the last component: between the listing and the open a runner can swap
    a PARENT directory for a symlink (`mv session-x y; ln -s /etc/jaah session-x`), and a vendor side
    file may carry any leaf name it likes, so a path check would read whatever this uid can read. The fd
    is what was opened; `host_platform.fd_path` names where it really is (`/proc/self/fd` on Linux).
    """
    fd = handle.fileno()
    st = os.fstat(fd)
    try:
        real = host_platform.fd_path(fd)
        root_real = os.path.realpath(root)
        inside = real != root_real and os.path.commonpath([real, root_real]) == root_real
    except (OSError, ValueError):
        inside = False
    if not inside or st.st_nlink != 1 or st.st_size > MAX_SOURCE_BYTES:
        raise OSError(errno_mod.EPERM, "%s is not a single-link file inside the vendor transcript root "
                      "within the read ceiling" % name)


def _assert_opened_in_place(handle, resolved: Path, name: str) -> None:
    """Windows: refuse an OPENED main transcript unless the handle's final path is `resolved` itself — the
    path `contained_file` resolved under its root. A no-op where `O_NOFOLLOW` exists.

    `open_no_follow`'s `lstat` + `st_ino` check there sees only the last component, so a parent swapped for a
    junction between the resolve and the open passes it and the open follows the junction. Compared with
    `resolved` as spelled, never re-resolved: a realpath now would follow the same junction."""
    if hasattr(os, "O_NOFOLLOW"):
        return
    try:
        same = os.path.normcase(host_platform.fd_path(handle.fileno())) == os.path.normcase(str(resolved))
    except OSError:
        same = False
    if not same:
        raise OSError(errno_mod.EPERM, "%s is not the transcript file it was resolved to" % name)


def prepare_member(
    name: str,
    source: Path,
    scratch: Path,
    *,
    whole_file: bool = False,
    analytics: Optional[Analytics] = None,
    contain: Optional[Path] = None,
    in_place: bool = False,
) -> MemberPrep:
    """Stream `source` → redact → hash → gzip into `scratch`, in ONE pass. Never touches `source`.

    `whole_file` decides the unit of redaction: a `.jsonl` transcript streams LINE BY LINE (a record
    is one physical line, so no pattern spans lines); an artifact is read WHOLE (a PEM block spans
    lines; they are kilobytes).

    `sha256` is over the post-redaction, pre-gzip bytes — what the operator's tar holds. Gzip uses
    `mtime=0`, so a retry rewrites identical bytes.

    `in_place`: `source` is a resolved main transcript, held to that path on Windows (`_assert_opened_in_place`).
    """
    digest = hashlib.sha256()
    raw_size = 0
    counts: Dict[str, int] = {}
    gz_path = scratch / name
    try:
        # A `vendor/…` name is a relative path (validated: no dot segment), so its directories are made.
        gz_path.parent.mkdir(parents=True, exist_ok=True)
        # Before any read: the gzipped-size check comes too late to prevent memory exhaustion.
        size = source.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise MemberUnreadable(
                "%s is %d bytes, above the %d-byte pre-gzip read ceiling"
                % (name, size, MAX_SOURCE_BYTES)
            )
        with open_no_follow(source) as handle, gz_path.open("wb") as out:
            if contain is not None:
                _assert_contained_fd(handle, contain, name)
            if in_place:
                _assert_opened_in_place(handle, source, name)
            with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz:
                if whole_file:
                    # Still chunked ("normally kilobytes" is not a bound); a PEM block straddling 4 MB is the
                    # accepted cost.
                    chunks: Iterable[bytes] = iter(lambda: handle.read(MAX_REDACT_CHUNK), b"")
                else:
                    # Capped `readline`: one tool-result record can be megabytes.
                    chunks = iter(lambda: handle.readline(MAX_REDACT_CHUNK), b"")
                for chunk in chunks:
                    redacted, hits = transcript_redact.redact_bytes(chunk)
                    transcript_redact.merge(counts, hits)
                    digest.update(redacted)
                    raw_size += len(redacted)
                    gz.write(redacted)
                    if analytics is not None:
                        # The RAW chunk: an over-reaching redaction must not silently zero token counts.
                        analytics.feed(chunk)
    except OSError as exc:
        # Errno and member name only: `str(OSError)` carries a path encoding `$JAAH_HOME` and the
        # account slug, and this reaches the operator's page.
        raise MemberUnreadable("%s could not be read (errno %s)" % (name, exc.errno), exc.errno)
    return MemberPrep(
        name, source, gz_path, gz_path.stat().st_size, raw_size, digest.hexdigest(), counts
    )


# ── The abandoned probe, bounded by hand ──────────────────────────────────────────────────────────

class LiveSessions:
    """The agents probe (`accounts_probe._default_agents_probe`) per ACCOUNT, **at most one fork per account
    per tick**.

    Outside the daemon's `AgentsProbeCache`, so memoised here: each probe is a 53-72 MB default-CLI process.
    FAIL-CLOSED: a probe that could not run records `None` ("not observed", never "no live sessions";
    `_default_agents_probe`). Not `runner_registry`'s pane pid: interactive sessions are never
    registered there.
    """

    __slots__ = ("_probe", "_cache")

    def __init__(self, probe=None) -> None:
        self._probe = probe or accounts_probe._default_agents_probe
        self._cache: Dict[str, Optional[frozenset]] = {}

    def for_account(self, config_dir: str) -> Optional[frozenset]:
        """The live session ids that account reports, or None when the probe could not be run."""
        if config_dir in self._cache:
            return self._cache[config_dir]
        result: Optional[frozenset] = None
        try:
            # `asyncio.run`: `get_event_loop()` with no running loop raises on 3.12.
            rc, out = asyncio.run(self._probe(config_dir))
        except Exception as exc:  # noqa: BLE001 — a probe that cannot run is "not observed", never empty
            log.warning("session_sweep: agents probe for %s could not run (%s)", config_dir, exc)
            self._cache[config_dir] = None
            return None
        if rc == 0:
            try:
                parsed = json.loads(out)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                result = frozenset(
                    str(entry.get("sessionId"))
                    for entry in parsed
                    if isinstance(entry, dict) and entry.get("sessionId")
                )
        self._cache[config_dir] = result
        return result

    def probe_count(self) -> int:
        return len(self._cache)


def _runs_binary(cmd: str, binary: str) -> Optional[bool]:
    """Whether a `CwdScan` argv summary is `binary` itself or an interpreter running it (`node …/<cli>`,
    interpreter flags skipped). None when the summary was truncated before it could say."""
    tokens = cmd.split()
    names = tokens[:1] + [t for t in tokens[1:] if not t.startswith("-")][:1]
    for token in names:
        name = os.path.basename(token)
        for suffix in (".js", ".mjs", ".cjs"):
            name = name[: -len(suffix)] if name.endswith(suffix) else name
        if name == binary:
            return True
    return None if cmd.endswith("…") else False


def _vendor_scan(worktree_path: str) -> Optional[processes.CwdScan]:
    """Gate 0's collecting scan of one tree, or None when any half of it could not be read.

    Stricter than the kill gates: `_merge_scans` keeps a local "occupied" when the session-user half
    fails (right for a kill), but here a readable `git` occupant beside an unreadable CLI would read the
    session GONE. So a failed delegated half is recorded and the whole scan is "not observed"."""
    failed: List[bool] = []

    def _resolve(wt_real, pids, *, collect=True):
        half = processes.resolve_cwds_as_session_user(wt_real, pids, collect=collect)
        if half.status == processes.PROBE_FAILED:
            failed.append(True)
        return half

    scan = processes.scan_cwd_processes(worktree_path, collect=True, resolve_unreadable=_resolve)
    return None if failed or scan.status == processes.PROBE_FAILED else scan


class VendorLiveSessions:
    """The vendor stand-in for the agents probe (`LiveSessions`): is a non-default session's CLI still running?

    Shaped like `LiveSessions.for_account` so `is_abandoned` judges both with ONE rule and ONE window.
    Live when the runner registry holds THIS session's live pane (pid AND start ticks), or a process
    rooted in the worktree runs the vendor binary — the scan is the authority (a registry that cannot be
    read is empty, never an error) and the only sighting of a console session. FAIL-CLOSED like the
    default probe: any unreadable half, unnamed occupant or capped occupant list is `None` ("not
    observed"), so a dead session in a tree of >25 occupants waits for them to go. Per worktree, not per
    session: another live CLI of the same vendor in the tree keeps an old session open (the safe side).
    One /proc scan per worktree per tick.
    """

    __slots__ = ("_registry", "_scan", "_scans")

    def __init__(self, registry=None, scan=None) -> None:
        self._registry = registry or runner_registry.live_in_worktree
        self._scan = scan or _vendor_scan
        self._scans: Dict[str, Optional[processes.CwdScan]] = {}

    def for_session(self, session_id: str, worktree_path: str) -> Optional[frozenset]:
        """`{session_id}` when live, empty when observed gone, None when it could not be observed."""
        note = session_vendor.read(session_id) or {}
        record = agent_providers.by_value(str(note.get("provider") or ""))
        if record is None or not worktree_path:
            return None
        live = frozenset((session_id,))
        try:
            ident = self._registry(worktree_path)
        except Exception as exc:  # noqa: BLE001 — defensive: one session, never the tick
            log.warning("session_sweep: runner registry lookup for %s raised (%s)", worktree_path, exc)
            return None
        if (ident is not None and ident.provider == record.value
                and ident.session_id in ("", session_id)):
            return live
        if worktree_path not in self._scans:
            try:
                self._scans[worktree_path] = self._scan(worktree_path)
            except Exception as exc:  # noqa: BLE001 — same rule
                log.warning("session_sweep: cwd scan of %s could not run (%s)", worktree_path, exc)
                self._scans[worktree_path] = None
        scan = self._scans[worktree_path]
        if scan is None or scan.status == processes.PROBE_FAILED:
            return None
        verdicts = [_runs_binary(str(p.get("cmd") or ""), record.binary) for p in scan.processes]
        if any(verdicts):
            return live
        # An occupant we could not name, or one past the cap, may be the CLI: not observed, not gone.
        if None in verdicts or scan.matched > len(scan.processes) or any(
                p.get("cmd") == processes.UNREADABLE_CMD for p in scan.processes):
            return None
        return frozenset()

    def probe_count(self) -> int:
        return len(self._scans)


def is_abandoned(
    live: Optional[frozenset], session_id: str, transcript: Optional[Path], now: float
) -> bool:
    """Whether an `open` session is really gone. BOTH halves are required, uniformly across OD2.

    Quiescence alone would end sessions idle between turns; the probe alone would end every session
    on an account whose probe failed.
    """
    if live is None or session_id in live:
        return False
    if transcript is None:
        return False
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) > QUIESCENT_SECONDS


# ── The reconcile leg ─────────────────────────────────────────────────────────────────────────────

def observed_sessions(
    config_dirs: Sequence[str],
    slot_dirs: Dict[str, str],
    floor: float,
    *,
    now: Optional[float] = None,
    limit: int = OBSERVED_BATCH,
) -> List[Dict[str, object]]:
    """Every session id this node can SEE, bounded by the horizon and by the OD2 fence.

    `config_dir` rides each item: we are the only party who knows it for a row nobody created. The
    horizon applies to the transcript's mtime, before anything is read.
    """
    stamp = now if now is not None else time.time()
    # Sorted NEWEST-first before the cap: a name-ordered cutoff over UUIDs would re-send the same 200 every
    # tick and never report the rest. Newest-first sheds the oldest, which the backfill owns.
    out: List[Dict[str, object]] = []
    for config_dir in config_dirs:
        if not is_a_plain_absolute_path(config_dir):
            log.warning(
                "session_sweep: config dir %r is not a plain absolute path — skipping it rather "
                "than sending a value the control plane would refuse for the whole batch", config_dir
            )
            continue
        projects = Path(config_dir) / "projects"
        try:
            # A context manager, so the iterator's descriptor is closed.
            with os.scandir(projects) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            # Normal: the roster is deliberately unfiltered by `is_dir()`.
            continue
        for entry in entries:
            slot_path = slot_dirs.get(entry.name)
            if slot_path is None or not entry.is_dir(follow_symlinks=False):
                continue
            out.extend(
                _observed_in_project(Path(entry.path), config_dir, slot_path, floor, stamp)
            )
    # `_mtime` is stripped before the wire: `ObservedItem` forbids extra fields.
    out.sort(key=lambda item: item.pop("_mtime"), reverse=True)
    if len(out) > limit:
        log.info(
            "session_sweep: %d observable session(s) exceed the %d-item batch — the NEWEST %d are "
            "reported and the oldest are left to the deferred backfill, not silently dropped",
            len(out), limit, limit,
        )
    return out[:limit]


def _observed_in_project(
    project_dir: Path, config_dir: str, slot_path: str, floor: float, now: float
) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    try:
        with os.scandir(project_dir) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError:
        return items
    for entry in entries:
        # `follow_symlinks=False`: a symlink would otherwise be observed and later drained.
        if not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
            continue
        session_id = entry.name[: -len(".jsonl")]
        if not _SESSION_ID.match(session_id):
            # Filtered here: one bad id 422s the whole batch.
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < floor:
            continue
        item: Dict[str, object] = {
            "session_id": session_id,
            "config_dir": config_dir,
            "worktree_path": slot_path,
            # Stripped by the caller's sort. Not sent.
            "_mtime": mtime,
        }
        # `started_at` picks the partition and `/work`'s sort key, so it comes from the transcript's
        # first record, never "now".
        started = _first_timestamp(Path(entry.path))
        if started is not None and _within_window(started, now):
            item["started_at"] = _iso(started)
        if _within_window(mtime, now):
            item["ended_at"] = _iso(mtime)
        items.append(item)
    return items


def _first_timestamp(path: Path) -> Optional[float]:
    """The first record's `timestamp`, as an epoch second. One line read, not the file."""
    try:
        with open_no_follow(path) as handle:
            # A bounded handful of lines: a transcript may open with a summary/meta line.
            for _ in range(20):
                raw = handle.readline(MAX_REDACT_CHUNK)
                if not raw:
                    break
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                stamp = _parse_epoch(str(obj.get("timestamp") or ""))
                if stamp is not None:
                    return stamp
    except OSError:
        return None
    return None


def _within_window(stamp: float, now: float) -> bool:
    """Mirrors `session_transcripts.within_the_collectable_window`; checked here because the CP's raise
    would 422 the whole batch."""
    return (now - OBSERVED_HORIZON.total_seconds()) <= stamp <= (
        now + CLOCK_SKEW_ALLOWANCE.total_seconds()
    )


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


# ── The drain, one session at a time ──────────────────────────────────────────────────────────────

def upload_session(
    item: Dict[str, object],
    config_dirs: Sequence[str],
    *,
    api=control_plane_api,
    put=put_object,
    scratch_root: Optional[Path] = None,
) -> Dict[str, object]:
    """Ticket, upload and report ONE session. Returns a small result record for the tick summary.

    SERIAL: the ticket takes a 15-minute lease, so ticketing the batch up front would drive the largest
    sessions to `gave_up` mid-upload. On retry every member is re-PUT to the same deterministic keys, so
    there is no partial-resume bookkeeping.
    """
    session_id = str(item.get("session_id") or "")
    worktree_path = str(item.get("worktree_path") or "")
    hint = item.get("config_dir")
    from agents import sdk_events  # noqa: PLC0415 — in-function, as `_vendor_transcript`: start-up stays light

    if item.get("agent_kind") == sdk_events.AGENT_KIND or item.get("runtime") == RUNTIME_ACP:
        # An SDK agent run, or any run the ACP host drove: its redacted event log is the transcript (§4.7), never
        # a vendor file under an account home (an ACP engine's own log, if it keeps one, is not looked for).
        return _upload_sdk_session(session_id, worktree_path, item, api=api, put=put, scratch_root=scratch_root)
    try:
        resolved = resolve_capture(session_id, worktree_path, hint if hint else None, config_dirs)
    except VendorLookupFailed as exc:
        # `failed` (retryable), never `unavailable` (terminal): nothing was learned. Bounded at five attempts.
        log.warning("session_sweep: %s — the per-vendor lookup could not be completed (%r); reporting "
                    "%s so the row stays retryable", session_id, exc, STATUS_FAILED)
        api.agent_api_session_report(session_id, {"status": STATUS_FAILED, "error": str(exc)})
        return {"session_id": session_id, "status": STATUS_FAILED, "members": 0}
    if resolved is None:
        # The bytes are gone (rolled window, released worktree): `unavailable`, terminal. Logged with the
        # ids, since a run of these means the roster or the cwd is wrong.
        log.warning(
            "session_sweep: %s has no transcript under any of this node's %d config dir(s) "
            "(cwd=%s) — reporting it unavailable",
            session_id, len(config_dirs), worktree_path,
        )
        api.agent_api_session_report(session_id, {
            "status": STATUS_UNAVAILABLE,
            "error": "no transcript for this session under any of this node's %d config dir(s)"
                     % len(config_dirs),
        })
        return {"session_id": session_id, "status": STATUS_UNAVAILABLE, "members": 0}

    transcript, config_dir, read_usage = resolved
    # `read_usage is None` means the default layout; a vendor's own record is listed by its adapter, never
    # by walking the default `<id>/subagents/` shape through a vendor tree.
    omitted: List[str] = []
    side_root: Optional[Path] = None
    if read_usage is None:
        subagents = subagent_members(transcript, session_id)
    else:
        subagents, omitted, side_root = vendor_session_members(session_id, transcript, config_dir)
    artifacts = artifact_members(worktree_path, item.get("dispatch_ref"))
    scratch = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=str(_scratch_root(scratch_root))))
    try:
        return _upload_prepared(
            session_id, transcript, subagents, artifacts, scratch, config_dir, api=api, put=put,
            read_usage=read_usage, omitted=omitted, side_root=side_root,
        )
    finally:
        # BY ABSOLUTE PATH, and only a directory this function created.
        shutil.rmtree(scratch, ignore_errors=True)


def _upload_sdk_session(session_id: str, worktree_path: str, item: dict, *, api, put,
                        scratch_root: Optional[Path]) -> Dict[str, object]:
    """`upload_session` for an SDK agent run or an ACP-hosted one: its events file (`agents/sdk_events.py`) is the
    one main member, priced line by line by `sdk_events.read_usage` (an ACP row's usage is its stream's, so the CP
    folds none of it); the task's prompts and artifacts ride as for every session. Everything after locating the
    file — redact, gzip, upload, the honest states — is shared. No file for a run whose stream never reached the
    CP (`runtime` not `acp`) is `not_applicable`: it never had one; for one that did, the file was lost."""
    from agents import sdk_events  # noqa: PLC0415 — as `upload_session`

    if not _SESSION_ID.match(session_id):
        log.warning("session_sweep: refusing a session id that cannot be a path segment: %r", session_id)
        api.agent_api_session_report(session_id, {"status": STATUS_UNAVAILABLE,
                                                  "error": "the session id cannot name an events file"})
        return {"session_id": session_id, "status": STATUS_UNAVAILABLE, "members": 0}
    transcript = None
    for path, root in sdk_events.candidates(session_id):
        transcript = contained_file(path, root)
        if transcript is not None:
            break
    if transcript is None:
        status = STATUS_UNAVAILABLE if item.get("runtime") == RUNTIME_ACP else STATUS_NOT_APPLICABLE
        api.agent_api_session_report(session_id, {
            "status": status,
            "error": "no events file for this run on this node" if status == STATUS_UNAVAILABLE
            else "the run recorded no event",
        })
        return {"session_id": session_id, "status": status, "members": 0}
    artifacts = artifact_members(worktree_path, item.get("dispatch_ref"))
    scratch = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=str(_scratch_root(scratch_root))))
    try:
        result = _upload_prepared(session_id, transcript, [], artifacts, scratch, "", api=api, put=put,
                                  read_usage=sdk_events.read_usage)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result.get("status") == STATUS_UPLOADED:
        sdk_events.drop_kept(session_id)    # a MicroVM run's kept copy has served its one purpose
    return result


def _upload_prepared(
    session_id: str,
    transcript: Path,
    subagents: List[Tuple[str, Path]],
    artifacts: List[Tuple[str, Path]],
    scratch: Path,
    config_dir: str,
    *,
    api,
    put,
    read_usage=None,
    omitted: Sequence[str] = (),
    side_root: Optional[Path] = None,
) -> Dict[str, object]:
    # `None` folds as the default layout; otherwise the vendor reader carried from `resolve_capture`. A
    # default-layout session run by another provider on the default CLI takes that adapter's per-line pricing tier.
    line_tier = claude_cli_line_tier(session_id) if read_usage is None else None
    analytics = Analytics(read_usage, line_tier=line_tier)
    counts: Dict[str, int] = {}
    prepared: List[MemberPrep] = []
    try:
        main = prepare_member(MAIN_TRANSCRIPT_MEMBER, transcript, scratch, analytics=analytics, in_place=True)
    except MemberUnreadable as exc:
        # The errno decides: GONE is `unavailable` (terminal); anything else `failed` (retryable, five
        # attempts), e.g. the EACCES before the readability converge reaches the file.
        gone = exc.errno in _ERRNO_GONE
        status = STATUS_UNAVAILABLE if gone else STATUS_FAILED
        # Logged with the path, which the reported error omits.
        log.warning(
            # `%r` on the path and `exc`: runner-chosen names may carry a newline, and the daemon re-emits
            # this line into its journal, where a raw one forges a record.
            "session_sweep: %s — the main transcript at %r could not be prepared (%r); reporting %s",
            session_id, str(transcript), exc, status,
        )
        api.agent_api_session_report(session_id, {"status": status, "error": str(exc)})
        return {"session_id": session_id, "status": status, "members": 0}
    if main.bytes > MAX_MEMBER_BYTES or main.bytes_raw > MAX_MEMBER_BYTES:
        # No main transcript means no capture: a genuine failure. Multipart above 100 MB is deferred.
        api.agent_api_session_report(session_id, {
            "status": STATUS_FAILED,
            # Both numbers: `ReportMemberIn` bounds `bytes_raw` at the same ceiling.
            "error": "the main transcript is %d gzipped / %d raw bytes, above the %d-byte ceiling"
                     % (main.bytes, main.bytes_raw, MAX_MEMBER_BYTES),
        })
        return {"session_id": session_id, "status": STATUS_FAILED, "members": 0}
    prepared.append(main)
    # The session's own model, snapshotted BEFORE subagents are folded into the same accumulator (else
    # the last subagent's model would win). `usage_by_model` still carries every model.
    session_model = analytics.model
    transcript_redact.merge(counts, main.redaction_counts)

    subagent_count = 0
    # Every omission is recorded: an unrecorded skip makes a truncated capture look complete.
    dropped: List[str] = list(omitted)
    scratch_bytes = main.bytes
    # For a vendor session the prompts and settings go FIRST: they are small and audit-critical, and a
    # vendor tree a runner can fill with spill files must not push them past the member limit or the
    # scratch budget. The default order is unchanged.
    ordered = artifacts + subagents if read_usage is not None else subagents + artifacts
    for name, path in ordered:
        if len(prepared) >= MEMBER_LIMIT:
            if not any(note.endswith("(member limit)") for note in dropped):
                log.warning(
                    "session_sweep: %s has more than %d members — %r and any after it are NOT uploaded",
                    session_id, MEMBER_LIMIT, name,
                )
            dropped.append("%s (member limit)" % name)
            continue
        if scratch_bytes >= MAX_SCRATCH_BYTES:
            # Whole-session scratch budget (`MAX_SCRATCH_BYTES`).
            log.warning(
                "session_sweep: %s reached the %d-byte scratch budget — %r is NOT uploaded",
                session_id, MAX_SCRATCH_BYTES, name,
            )
            dropped.append("%s (scratch budget)" % name)
            continue
        try:
            # Subagent transcripts are SPEND folded into the same `analytics`: their turns never appear in
            # the main file (measured up to ~half the output tokens missing otherwise). No double count:
            # the key `(message.id, requestId)` is per response, and results re-enter main as ignored
            # `user` lines. Artifacts and `vendor/` files carry no `usage` and are not folded (a vendor's
            # session log is JSONL too, which is why this keys on the prefix and not the suffix).
            fold_into = analytics if name.startswith("subagent-") else None
            prep = prepare_member(name, path, scratch, whole_file=not name.endswith(".jsonl.gz"),
                                  analytics=fold_into,
                                  contain=side_root if (name, path) in subagents else None)
        except MemberUnreadable as exc:
            # One vanished member must not cost the session. Logged, skipped.
            log.warning("session_sweep: %s — %s", session_id, exc)
            dropped.append("%s (unreadable)" % name)
            continue
        if prep.bytes > MAX_MEMBER_BYTES or prep.bytes_raw > MAX_MEMBER_BYTES:
            log.warning(
                "session_sweep: %s member %r is %d gzipped / %d raw bytes, above the %d-byte "
                "ceiling — it is SKIPPED so the rest of the session still uploads",
                session_id, name, prep.bytes, prep.bytes_raw, MAX_MEMBER_BYTES,
            )
            dropped.append("%s (over the size ceiling)" % name)
            continue
        prepared.append(prep)
        scratch_bytes += prep.bytes
        transcript_redact.merge(counts, prep.redaction_counts)
        if name.startswith("subagent-"):
            subagent_count += 1

    ticket = api.agent_api_session_upload_ticket(
        session_id, [{"name": prep.name, "bytes": prep.bytes} for prep in prepared]
    )
    if not ticket:
        return {"session_id": session_id, "status": "no_ticket", "members": len(prepared)}
    urls = {entry.get("name"): entry.get("url") for entry in (ticket.get("urls") or [])}

    tokens_in, tokens_out = analytics.totals()
    usage_by_model = analytics.usage_by_model()
    measurements: Dict[str, object] = {
        "subagent_count": subagent_count,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model": session_model,
        # The priceable per-model breakdown, off the same fold as the totals.
        "usage_by_model": usage_by_model,
        # A rejected usage carrier means some spend may be absent even when other turns were valid.
        "usage_incomplete": analytics.reader_errors > 0,
        "explored_targets": analytics.explored_targets(),
        "redaction": transcript_redact.report(counts),
    }
    if dropped:
        # On `redaction`: the report's one free-form field (8192 bytes CP-side), copied into the manifest.
        measurements["redaction"]["members_not_uploaded"] = _bounded_notes(dropped)  # type: ignore[index]
    for prep in prepared:
        url = urls.get(prep.name)
        if not url:
            # No URL for a named member: reported, never uploaded partially and called `uploaded`.
            payload = dict(measurements)
            payload.update({
                "status": STATUS_FAILED,
                "error": "the upload ticket carried no URL for member %r" % prep.name,
            })
            api.agent_api_session_report(session_id, payload)
            return {"session_id": session_id, "status": STATUS_FAILED, "members": len(prepared)}
        try:
            # `prep.bytes`, the gzipped size already reported, so socket and record agree.
            put(url, prep.gz_path, size=prep.bytes)
        except Exception as exc:  # noqa: BLE001 — every transport failure is one retryable outcome
            payload = dict(measurements)
            payload.update({
                "status": STATUS_FAILED,
                # Never the URL (a bearer token).
                "error": "uploading %r failed: %s: %s" % (prep.name, type(exc).__name__, exc),
            })
            api.agent_api_session_report(session_id, payload)
            return {"session_id": session_id, "status": STATUS_FAILED, "members": len(prepared)}

    payload = dict(measurements)
    payload.update({
        "status": STATUS_UPLOADED,
        "members": [prep.wire() for prep in prepared],
    })
    api.agent_api_session_report(session_id, payload)
    log.info(
        # `model=%r`: a runner-written name with a newline would forge a journal record. `models` keys are
        # clamped, and a dict's repr escapes them.
        "session_sweep: %s uploaded %d member(s) (%d subagent(s)) from %s — in=%s out=%s model=%r "
        "models=%s%s%s",
        session_id, len(prepared), subagent_count, config_dir, tokens_in, tokens_out, session_model,
        # Every model the session spent on.
        ",".join(sorted(usage_by_model)) or "-",
        # The refused-line count, only when non-zero (`_feed_vendor` logs just the first).
        ("" if not analytics.reader_errors
         else " unreadable_lines=%d/%d" % (analytics.reader_errors, analytics.lines)),
        # Spending lines a hand-off's usage window left to the other front, only when non-zero.
        ("" if not analytics.window_dropped
         else " window_dropped=%d/%d" % (analytics.window_dropped, analytics.lines)),
    )
    return {
        "session_id": session_id,
        "status": STATUS_UPLOADED,
        "members": len(prepared),
        "subagent_count": subagent_count,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model": session_model,
    }


# ── The live leg: a RUNNING session's usage so far ────────────────────────────────────────────────
#
# The drain leg folds usage only after a session ENDS, so a runner's spend is invisible on the Tasks board
# for its whole run. This leg folds an `open`, live, task-bound session's transcripts as they stand and
# posts the same derived values the transcript report carries; the final upload's figure supersedes it.
#
# COST: each post re-reads the main transcript and every subagent file WHOLE, under the drain leg's own
# budgets (`MAX_SOURCE_BYTES` per file, `MEMBER_LIMIT` members, `MAX_SCRATCH_BYTES` in all). What bounds
# the rate is the throttle: at most one fold per session per `LIVE_USAGE_INTERVAL`, and none while the
# files' sizes and mtimes are unchanged (an idle runner costs a few `stat`s). No scratch, no gzip, no
# redaction: only the counts leave the node.

def live_usage_path() -> Path:
    """`<state_dir>/sessions/live-usage.json` — `{session_id: {"at": epoch, "sig": fingerprint}}`, the
    throttle across ticks of this one-shot. Losing it costs one extra post per session, nothing more."""
    return env_helpers.state_dir("sessions", "live-usage.json")


def _load_live_usage_state(path: Path) -> Dict[str, Dict[str, object]]:
    """The throttle, or `{}`. Opened with `open_no_follow`: a symlink planted at the path is refused (read
    as empty), never followed into a file the daemon can read and the runner cannot."""
    try:
        with open_no_follow(path) as handle:
            raw = handle.read(1024 * 1024)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("session_sweep: the live-usage throttle %s is unreadable (%s) — treating it as empty",
                    path, exc)
        return {}
    try:
        state = json.loads(raw)
    except ValueError:
        log.warning("session_sweep: the live-usage throttle %s is corrupt — treating it as empty", path)
        return {}
    if not isinstance(state, dict):
        log.warning("session_sweep: the live-usage throttle %s is not an object — treating it as empty", path)
        return {}
    return {str(k): v for k, v in state.items() if isinstance(v, dict)}


def _save_live_usage_state(path: Path, state: Dict[str, Dict[str, object]]) -> None:
    """`env_helpers.atomic_write_text`: a `mkstemp` name and `os.replace`, so a symlink planted at the path
    (or any predictable temp name) is replaced, never written through. An unwritable state dir is logged,
    never raised: the worst case is a post every tick whose files changed, which the fingerprint bounds."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        env_helpers.atomic_write_json(path, state, mode=0o600, sort_keys=True)
    except OSError as exc:
        log.warning("session_sweep: could not persist the live-usage throttle to %s (%s)", path, exc)


def _live_usage_members(
    session_id: str, transcript: Path, read_usage, config_dir: str
) -> Tuple[List[Tuple[str, Path]], Optional[Path]]:
    """`([(member name, path)], containment root)` — the drain leg's own listing, IN ITS ORDER: the default
    layout's `<id>/subagents/agent-*.jsonl`, or every member a vendor's adapter lists (only its `subagent-*` ones are
    folded, but the rest take member slots exactly as they do in the upload)."""
    if read_usage is None:
        return subagent_members(transcript, session_id), None
    members, _notes, side_root = vendor_session_members(session_id, transcript, config_dir)
    return members, side_root


def _live_usage_signature(transcript: Path, members: Sequence[Tuple[str, Path]]) -> str:
    """Sizes and mtimes of every file the fold reads — `stat` only, so an unchanged session is free."""
    parts: List[object] = []
    for name, path in [(MAIN_TRANSCRIPT_MEMBER, transcript)] + list(members):
        if name != MAIN_TRANSCRIPT_MEMBER and not name.startswith("subagent-"):
            continue
        try:
            st = path.stat()
            parts.append([name, st.st_size, st.st_mtime_ns])
        except OSError:
            parts.append([name, None, None])
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _fold_file(analytics: Analytics, path: Path, name: str, *, contain: Optional[Path] = None,
               single_link: bool = False, in_place: bool = False) -> int:
    """Feed one transcript into `analytics` raw, as `prepare_member` does minus the scratch copy, and return
    the bytes read. Raises `OSError` for a file that cannot be read, is over the read ceiling, or (with
    `single_link`) has a second name — N links to one big file would multiply the read N times.

    Stops at the last COMPLETE line: a running transcript's tail may be half-written, and a torn line fed
    to a vendor reader raises and flips `usage_incomplete` mid-run. A capped `readline` fragment of one
    long line is still fed, as the drain leg feeds it; only an unterminated LAST chunk is held back."""
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise OSError(errno_mod.EFBIG, "%s is %d bytes, above the read ceiling" % (name, size))
    read = 0
    with open_no_follow(path) as handle:
        if contain is not None:
            _assert_contained_fd(handle, contain, name)
        if in_place:
            _assert_opened_in_place(handle, path, name)
        if single_link and os.fstat(handle.fileno()).st_nlink != 1:
            raise OSError(errno_mod.EPERM, "%s has more than one link" % name)
        # Capped `readline`: one tool-result record can be megabytes.
        chunk = handle.readline(MAX_REDACT_CHUNK)
        while chunk:
            read += len(chunk)
            following = handle.readline(MAX_REDACT_CHUNK)
            if chunk.endswith(b"\n") or following:
                analytics.feed(chunk)
            chunk = following
    return read


def live_usage(
    session_id: str,
    transcript: Path,
    members: Sequence[Tuple[str, Path]],
    read_usage=None,
    side_root: Optional[Path] = None,
) -> Optional[Dict[str, object]]:
    """The `POST …/usage` body for the main transcript plus the `subagent-*` `members`, or None when the
    main transcript cannot be read (the drain leg then reports no usage either).

    Mirrors `_upload_prepared`'s member policy so the live figure converges on the final one: members in
    the drain leg's order, each successful one taking a slot of `MEMBER_LIMIT` (main takes the first), the
    budget checked BEFORE each member and charged after it, an unreadable or over-ceiling member skipped.
    Every skip marks `usage_incomplete`. Remaining divergences, all from not writing a scratch copy: the
    byte budget is charged RAW bytes of the folded files only (the drain leg charges GZIPPED bytes of every
    member, artifacts included, so it reaches its budget later), and a vendor session's `_meta` artifacts,
    which the drain leg places first, take no slot here. Both only bite a session at the limits, where the
    final figure is itself `members_not_uploaded`-annotated."""
    line_tier = claude_cli_line_tier(session_id) if read_usage is None else None
    analytics = Analytics(read_usage, line_tier=line_tier)
    try:
        used = _fold_file(analytics, transcript, MAIN_TRANSCRIPT_MEMBER, in_place=True)
    except OSError as exc:
        log.warning("session_sweep: %s — the main transcript could not be folded live (errno %s)",
                    session_id, exc.errno)
        return None
    slots = 1
    skipped = 0
    for name, path in members:
        if slots >= MEMBER_LIMIT or used >= MAX_SCRATCH_BYTES:
            if name.startswith("subagent-"):
                skipped += 1
            continue
        if not name.startswith("subagent-"):
            # A vendor side file: uploaded, never folded, but it takes its slot as in the drain leg.
            slots += 1
            continue
        try:
            used += _fold_file(analytics, path, name, contain=side_root,
                               single_link=side_root is None)
        except OSError as exc:
            log.warning("session_sweep: %s — %r skipped from the live fold (errno %s)",
                        session_id, name, exc.errno)
            skipped += 1
            continue
        slots += 1
    tokens_in, tokens_out = analytics.totals()
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usage_by_model": analytics.usage_by_model(),
        "usage_incomplete": analytics.reader_errors > 0 or skipped > 0,
        "explored_targets": analytics.explored_targets(),
    }


def post_live_usage(
    item: Dict[str, object],
    transcript: Path,
    config_dir: str,
    read_usage,
    state: Dict[str, Dict[str, object]],
    now: float,
    *,
    api,
) -> bool:
    """Fold and post one LIVE `open` session's usage when it is task-bound, its last post is at least
    `LIVE_USAGE_INTERVAL` old and its files changed since. Updates `state`; True when the CP took it."""
    from agents import sdk_events  # noqa: PLC0415 — as `upload_session`

    session_id = str(item.get("session_id") or "")
    # A console session has no task to price. An SDK agent run (the CP names it by `agent_kind`) has no
    # account-home transcript for `resolve_capture` to find; its events file is folded only at upload.
    if item.get("task_id") is None or item.get("agent_kind") == sdk_events.AGENT_KIND:
        return False
    entry = state.get(session_id) or {}
    last_at = entry.get("at")
    if isinstance(last_at, (int, float)) and now - last_at < LIVE_USAGE_INTERVAL:
        return False
    members, side_root = _live_usage_members(session_id, transcript, read_usage, config_dir)
    sig = _live_usage_signature(transcript, members)
    if sig == entry.get("sig"):
        return False
    payload = live_usage(session_id, transcript, members, read_usage, side_root)
    if payload is None:
        return False
    posted = api.agent_api_session_usage(session_id, payload)
    # A failed post still stamps `at` (keeping the old `sig`), so a down CP is retried once per interval
    # rather than re-read every tick.
    state[session_id] = {"at": now, "sig": sig if posted else entry.get("sig")}
    return bool(posted)


# ── The pane-log leg: a dead run's last screenfuls ────────────────────────────────────────────────

# The wire cap: the LAST this-many bytes of the pane log; the CP trims further after cleaning.
PANE_LOG_TAIL_BYTES = 65536
# The only `dispatch_ref` shape a fleet runner's pane log carries (`fleet_dispatch`'s `task-<id>`). Anything
# else is never globbed: the ref is CP-supplied and lands inside a glob pattern.
_PANE_LOG_REF = re.compile(r"\Atask-\d+\Z")


def pane_log_tail(dispatch_ref: str, log_root: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """`(tail, written_at)` of the newest `*-<dispatch_ref>.pane.log` — `(None, None)` when there is none.

    `fleet_dispatch` tees the pane to `<repo or project>-task-<id>.pane.log`, appended across every run of
    that task, so the newest mtime wins and only the end matters. RAISES ValueError on a non-`task-<id>`
    ref (the caller posts nothing). The runner uid owns that dir, so a symlink is refused at the syscall
    (`open_no_follow`) and a bigger file never costs more than `PANE_LOG_TAIL_BYTES` of memory."""
    if not _PANE_LOG_REF.match(dispatch_ref or ""):
        raise ValueError("not a task-<id> dispatch_ref: %r" % (dispatch_ref,))
    root = log_root if log_root is not None else env_helpers.log_dir("runner")
    newest: Optional[Tuple[float, Path]] = None
    # The fixed `-` before the ref: `task-1` must not match `...-task-11.pane.log`.
    for candidate in root.glob("*-%s.pane.log" % dispatch_ref):
        try:
            mtime = candidate.lstat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, candidate)
    if newest is None:
        return None, None
    with open_no_follow(newest[1]) as handle:
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - PANE_LOG_TAIL_BYTES))
        data = handle.read(PANE_LOG_TAIL_BYTES)
    return data.decode("utf-8", errors="replace"), _iso(newest[0])


# ── One tick ──────────────────────────────────────────────────────────────────────────────────────

def sweep_once(
    *,
    node_id: Optional[str] = None,
    config_dirs: Optional[Sequence[str]] = None,
    worktree_paths: Optional[Sequence[str]] = None,
    horizon_file: Optional[Path] = None,
    now: Optional[float] = None,
    probe=None,
    vendor_probe: Optional[VendorLiveSessions] = None,
    api=control_plane_api,
    put=put_object,
    scratch_root: Optional[Path] = None,
    live_usage_file: Optional[Path] = None,
) -> Dict[str, object]:
    """Drain, reconcile, probe and post pane logs once. Every dependency is injectable so the tick is testable whole.

    Best-effort: a per-session failure is reported and the tick continues, never starving the rest.
    """
    node = node_id if node_id is not None else control_plane_api.this_node_id()
    if not node:
        log.warning("session_sweep: NODE_ID is unset — this node cannot be attributed, skipping")
        return {"skipped": "no_node_id"}
    stamp = now if now is not None else time.time()
    roster = list(config_dirs) if config_dirs is not None else accounts_probe._agent_config_dirs()

    # First: a tick killed by the task timeout leaves scratch only a fresh subprocess can reap.
    reap_orphan_scratch(scratch_root)

    # Notes age out here, the pass that reads them. The bound derives from `OBSERVED_HORIZON` (the oldest
    # session still reportable): while storage is unavailable nothing drains for weeks, and a shorter
    # bound would prune notes before their sessions could be drained.
    session_vendor.prune(older_than=OBSERVED_HORIZON.total_seconds() + _PRUNE_MARGIN_SECONDS, now=stamp)
    work = api.agent_api_sessions_work(node)
    if work is None:
        log.warning("session_sweep: the work list is unavailable — nothing is attempted this tick")
        return {"skipped": "work_unavailable"}

    result: Dict[str, object] = {
        "node_id": node, "config_dirs": len(roster), "drained": [], "observed": 0, "ended": 0,
        "storage_available": bool(work.get("storage_available")),
    }

    # 1. DRAIN — serially. `awaiting_transcript` is empty whenever `storage_available` is false.
    for item in work.get("awaiting_transcript") or []:
        try:
            result["drained"].append(  # type: ignore[union-attr]
                upload_session(item, roster, api=api, put=put, scratch_root=scratch_root)
            )
        except Exception as exc:  # noqa: BLE001 — one session may not cost the rest of the tick
            log.warning(
                "session_sweep: %s could not be uploaded (%s)", item.get("session_id"), exc,
                exc_info=True,
            )
            # Recorded, so the summary does not undercount failures.
            result["drained"].append({  # type: ignore[union-attr]
                "session_id": item.get("session_id"), "status": "error",
                "error": type(exc).__name__,
            })

    # 2. RECONCILE — the half a list-draining sweep structurally cannot do.
    paths = list(worktree_paths) if worktree_paths is not None else _pool_worktree_paths(node)
    if paths is None:
        log.warning(
            "session_sweep: this node's pool listing is unavailable, so the OD2 fence is unknown — "
            "the reconcile leg is skipped this tick rather than run against an empty fence"
        )
    else:
        floor = horizon_floor(horizon_file, stamp)
        result["horizon"] = floor
        items = observed_sessions(roster, slot_project_dirs(paths), floor, now=stamp)
        if items:
            api.agent_api_sessions_observed(node, items)
        result["observed"] = len(items)

    # 3. PROBE — is a session the CP still holds `open` actually gone?
    live = LiveSessions(probe)
    vendor_live = vendor_probe if vendor_probe is not None else VendorLiveSessions()
    ended = 0
    # The live leg's throttle, read and pruned to this tick's `open` list once, written back once.
    open_items = work.get("open") or []
    usage_file = live_usage_file or live_usage_path()
    loaded = _load_live_usage_state(usage_file)
    open_ids = {str(item.get("session_id") or "") for item in open_items}
    usage_state = {sid: entry for sid, entry in loaded.items() if sid in open_ids}
    live_posts = 0
    for item in open_items:
        session_id = str(item.get("session_id") or "")
        worktree_path = str(item.get("worktree_path") or "")
        hint = item.get("config_dir")
        try:
            found = resolve_capture(session_id, worktree_path, hint if hint else None, roster)
        except Exception as exc:  # noqa: BLE001 — the drain leg guards each session; this did not
            # One session's problem, not the tick's.
            log.warning("session_sweep: probing %s raised (%s: %r) — skipping it, not the tick",
                        session_id, type(exc).__name__, exc)
            continue
        if found is None:
            continue
        transcript, config_dir, read_usage = found
        # A vendor session is NEVER asked the default agents probe, which answers `[]` (not `None`) on a vendor
        # account and would end a live runner on quiescence alone; its own probe answers instead.
        if read_usage is None:
            live_ids = live.for_account(config_dir)
        elif is_abandoned(frozenset(), session_id, transcript, stamp):
            # Quiet first: the /proc walk is paid only for a session that could be over.
            live_ids = vendor_live.for_session(session_id, worktree_path)
        else:
            # Not quiet: live without asking (`is_abandoned(None, …)` is False).
            live_ids = None
        if not is_abandoned(live_ids, session_id, transcript, stamp):
            # LIVE: its usage so far, throttled (`post_live_usage`).
            try:
                if post_live_usage(item, transcript, config_dir, read_usage, usage_state, stamp, api=api):
                    live_posts += 1
            except Exception as exc:  # noqa: BLE001 — a live figure must never cost the tick
                log.warning("session_sweep: the live usage of %s could not be posted (%s: %r)",
                            session_id, type(exc).__name__, exc)
            continue
        quiet_since: Optional[str] = None
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            # Rotated away since: still over, reported without a WHEN.
            mtime = None
        # The observed leg's window guard: `SessionEndIn` raises on an out-of-window `ended_at`, which would
        # 422 every tick forever; an absent one is valid.
        if mtime is not None and _within_window(mtime, stamp):
            quiet_since = _iso(mtime)
        # Counted only when the CP accepted it.
        if api.agent_api_session_end(session_id, END_REASON_ABANDONED, quiet_since):
            ended += 1
    result["ended"] = ended
    result["live_usage"] = live_posts
    if usage_state != loaded:
        _save_live_usage_state(usage_file, usage_state)
    result["probes"] = live.probe_count()
    result["vendor_probes"] = vendor_live.probe_count()

    # 4. PANE LOG — an unexplained end's raw tail. Absent on an older CP; served regardless of storage.
    pane_logs = 0
    for item in work.get("awaiting_pane_log") or []:
        session_id = str(item.get("session_id") or "")
        try:
            try:
                tail, written_at = pane_log_tail(str(item.get("dispatch_ref") or ""))
            except (ValueError, OSError) as exc:
                # Permanent here (a ref no pane log is named by, a refused symlink, an unreadable file): answered
                # as "no pane log", or the CP would offer it every tick for its whole window.
                log.warning("session_sweep: no readable pane log for %s (%s: %r)",
                            session_id, type(exc).__name__, exc)
                tail, written_at = None, None
            # Counted only when the CP accepted it; a `None` tail is a real answer ("missing").
            if api.agent_api_session_pane_log(session_id, tail, written_at):
                pane_logs += 1
        except Exception as exc:  # noqa: BLE001 — one session's pane log may not cost the tick
            log.warning("session_sweep: the pane log of %s could not be posted (%s: %r)",
                        session_id, type(exc).__name__, exc)
    result["pane_logs"] = pane_logs
    log.info(
        "session_sweep: node=%s storage=%s drained=%d [%s] observed=%d ended=%d live_usage=%d probes=%d "
        "vendor_probes=%d pane_logs=%d",
        node, result["storage_available"], len(result["drained"]),  # type: ignore[arg-type]
        _drain_tally(result["drained"]),  # type: ignore[arg-type]
        result["observed"], ended, live_posts, live.probe_count(), vendor_live.probe_count(), pane_logs,
    )
    return result


def _drain_tally(drained: Sequence[Dict[str, object]]) -> str:
    """`uploaded=8 unavailable=2` for a tick's drain results — `-` when nothing was attempted.

    A bare count cannot tell ten uploads from ten `unavailable`s. Sorted, so ticks diff by eye."""
    if not drained:
        return "-"
    counts: Dict[str, int] = {}
    for entry in drained:
        status = str(entry.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return " ".join("%s=%d" % (status, counts[status]) for status in sorted(counts))
