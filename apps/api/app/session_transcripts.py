"""Where one session's transcript lives in S3, and the manifest that describes it — the CP's half.

The CP derives every key; a node names only a member, and a name outside `member_root`'s allowlist is
refused. A key accepted off the wire would let a node write an arbitrary bucket path.

Two top-level prefixes because an Athena table over `transcripts/dt=…` scans every object in the
partition, so `manifest.json` cannot sit beside the JSONL. `dt` is the UTC date of the row's
`started_at`, never a clock, so a retry overwrites the same objects.

Pure computation: no clock, I/O or DB (rows: `app/agent_session_lifecycle.py`; signing:
`app/aws/presign.py`).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.clock import as_utc, utc_iso_or_none
from app.node_clone import is_absolute_node_path, node_path_segments

# The two roots; the Athena argument above requires them to differ.
TRANSCRIPT_ROOT = "transcripts"
ARTIFACT_ROOT = "artifacts"

# Plain, not gzipped: the assembler needs every member's `bytes_raw` before writing a tar header.
MANIFEST_NAME = "manifest.json"

# Bumped when the manifest's shape changes, so a reader can refuse what it cannot parse.
MANIFEST_SCHEMA = 1

# A caveat, never assurance: `hits` counts what was caught, not what was missed. Also the manifest's
# fallback when a node reports no redaction block. Spelled a second time in
# `apps/jaahd/transcript_redact.py::CONFIDENCE` (a stdlib-only deployable that cannot import this);
# the manifest round-trip enforces the pairing.
REDACTION_CONFIDENCE = "best_effort"

# ── The lease machine's two numbers ───────────────────────────────────────────────────────────────
# URLs expire with the lease: a longer lease holds a row it cannot upload, a shorter one lets a second
# ticket race the first's still-valid PUTs onto the same keys.
LEASE_TTL = timedelta(minutes=15)
LEASE_TTL_SECONDS = int(LEASE_TTL.total_seconds())

# Then `gave_up`: terminal, and distinct from `failed` so a row nothing will retry does not read as retrying.
MAX_TRANSCRIPT_ATTEMPTS = 5

# Rows per `/work` tick. Processed serially, so this bounds a tick's work, not up-front leases.
WORK_BATCH = 10

# Running sessions per `/work` tick for the aliveness probe. Larger than the upload batch because
# probing is cheap and a backlog is exactly what the probe is for; bounded since the payload is per-tick.
OPEN_PROBE_BATCH = 100

# `/sessions/observed` cap: a second fence so one misconfigured node horizon cannot post a whole disk.
OBSERVED_BATCH = 200

# How far back a node-reported timestamp may reach. `started_at` picks the `dt=` partition and is
# `/work`'s sort key, so an unclamped one jumps every batch. Generous versus the node's days-long window.
OBSERVED_HORIZON = timedelta(days=90)
# Slack ahead of the CP clock for ordinary drift.
CLOCK_SKEW_ALLOWANCE = timedelta(minutes=5)


def within_the_collectable_window(value: datetime, now: datetime) -> bool:
    """Whether a node-reported timestamp describes a session the fleet could actually have run."""
    stamp = as_utc(value)
    return (now - OBSERVED_HORIZON) <= stamp <= (now + CLOCK_SKEW_ALLOWANCE)


# `cwd` is relayed back on `/work` and resolved under by the receiving node, so an unvalidated value
# lets one node choose another's read target. It never reaches a key, hence a shape check.
def is_a_plain_absolute_path(value: str) -> bool:
    """Whether a path is absolute on some node OS, free of `..` segments, and free of control characters.

    "Absolute" and "segment" are `node_clone`'s (a posix `/…` or a Windows drive path; `..`
    under either separator): a posix-only copy here 422'd every Windows node's session report."""
    if not is_absolute_node_path(value) or ".." in node_path_segments(value):
        return False
    return not any(character < " " or character == "\x7f" for character in value)


# Most members per request: bounds how many URLs one request can make the CP presign or serialize.
MEMBER_LIMIT = 256

# ── What a member may be called ───────────────────────────────────────────────────────────────────
# The allowlist is the guard: each name maps to exactly one root and key. `manifest.json` is absent
# because the CP writes it; a node may not overwrite the integrity record.
MAIN_TRANSCRIPT_MEMBER = "main.jsonl.gz"
_SUBAGENT_MEMBER = re.compile(r"\Asubagent-[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.jsonl\.gz\Z")
_ARTIFACT_MEMBERS = frozenset({"system-prompt.md.gz", "task-prompt.md.gz", "settings.json.gz"})
#: The fixed artifact names, public for a reader deriving every key a session can hold (tenant offboarding).
ARTIFACT_MEMBERS = _ARTIFACT_MEMBERS
# A non-Claude vendor's OTHER session files, redacted like every member (a spilled tool output, a session
# log, a session dir's contents): `vendor/<path under the vendor's transcript root>.gz`. Stored under `artifacts/`, not
# `transcripts/` — most are not JSONL, and the Athena table reads every line under `transcripts/`. Each
# segment starts alphanumeric, so no `.`/`..` segment and no empty one. Real names are long (one vendor's
# spill `<slug>/tool-outputs/session-<uuid>/run_shell_command__run_shell_command_<ms>_0.txt` measured 136
# characters), hence `MEMBER_NAME_MAX`; the archive's PAX headers carry any length.
_VENDOR_MEMBER = re.compile(
    r"\Avendor/(?:[A-Za-z0-9][A-Za-z0-9._-]*/){0,6}[A-Za-z0-9][A-Za-z0-9._-]*\.gz\Z"
)
# The longest member name the ticket and report accept (`routes/agent_sessions.py`).
MEMBER_NAME_MAX = 255


def is_vendor_member(name: str) -> bool:
    """Whether `name` is a `vendor/…` member the control plane issues a key for."""
    return len(name) <= MEMBER_NAME_MAX and bool(_VENDOR_MEMBER.match(name))

# Manifest member kinds, derived here (the assembler rebuilds the key from `kind`), never accepted.
KIND_TRANSCRIPT = "transcript"
KIND_ARTIFACT = "artifact"

# A session id must be safe as a path segment: `agent_session_id` is unconstrained TEXT. `\A`/`\Z`,
# never `^`/`$`: `$` matches before a trailing newline, which would forge line breaks in logs.
_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# The same guard for the ingest that creates a row: an unkeyable row would 422 every ticket without
# moving, and ten of them starve a node's whole batch forever. Refuse at the door.
def is_usable_session_id(value: object) -> bool:
    """Whether `value` can ever become an object-key segment — so whether it may become a row."""
    return isinstance(value, str) and bool(_SESSION_ID.match(value))

# Guards the node-reported size, which reaches a tar header (100 MB: the multipart floor).
MAX_MEMBER_BYTES = 100 * 1024 * 1024


class KeyRefused(ValueError):
    """A key could not be derived — a member name outside the allowlist, or an unusable session id.

    Its own type so a route can 422 exactly this and leave real bugs a 500."""


def reject_supplied_key(payload: Mapping[str, Any]) -> None:
    """Refuse a request that tries to name an object key itself.

    Run it on the raw request body, never a parsed model: Pydantic strips unknown fields first, so a
    guard on `model_dump()` sees nothing. Callers wire it as a `mode="before"` validator
    (`routes/agent_sessions.py`).
    """
    for field in ("key", "keys", "prefix", "bucket", "manifest_key", "transcript_key"):
        if field in payload:
            raise KeyRefused(
                f"the control plane derives every object key; `{field}` may not be supplied"
            )


def partition_date(started_at: datetime) -> str:
    """The `dt=` partition value for a row: the UTC date of its `started_at`, as `YYYY-MM-DD`.

    A naive datetime is read as UTC; local time would silently shift the partition.
    """
    if not isinstance(started_at, datetime):
        raise KeyRefused("started_at must be a datetime")
    return as_utc(started_at).astimezone(timezone.utc).strftime("%Y-%m-%d")


def _session_segment(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        raise KeyRefused(
            "session id is not usable as an object-key segment (letters, digits, `.`, `-`, `_`; "
            "at most 128 characters)"
        )
    if session_id in (".", ".."):  # unreachable through the pattern; kept if the pattern ever moves
        raise KeyRefused("session id may not be a dot segment")
    return session_id


def member_root(name: str) -> str:
    """Which top-level prefix `name` belongs under — the allowlist, and the whole key guard.

    Raises `KeyRefused` for anything else, `manifest.json` included (CP-authored).
    """
    if not isinstance(name, str) or not name:
        raise KeyRefused("member name is required")
    if name == MAIN_TRANSCRIPT_MEMBER or _SUBAGENT_MEMBER.match(name):
        return TRANSCRIPT_ROOT
    if name in _ARTIFACT_MEMBERS or is_vendor_member(name):
        return ARTIFACT_ROOT
    raise KeyRefused(
        f"member name {name!r} is not one the control plane issues a key for "
        f"(`{MAIN_TRANSCRIPT_MEMBER}`, `subagent-<id>.jsonl.gz`, `vendor/<path>.gz`, "
        + ", ".join(f"`{member}`" for member in sorted(_ARTIFACT_MEMBERS))
        + ")"
    )


def member_kind(name: str) -> str:
    """`transcript` or `artifact` for `name` — the manifest's own `kind`, derived, never accepted."""
    return KIND_TRANSCRIPT if member_root(name) == TRANSCRIPT_ROOT else KIND_ARTIFACT


def transcript_prefix(*, session_id: str, started_at: datetime) -> str:
    """`transcripts/dt=<date>/<session_id>/` — the folder `sessions.transcript_prefix` names.

    The trailing slash keeps `…/<sid>` from prefix-matching `…/<sid>-2` in an S3 list.
    """
    return f"{TRANSCRIPT_ROOT}/dt={partition_date(started_at)}/{_session_segment(session_id)}/"


def artifact_prefix(*, session_id: str, started_at: datetime) -> str:
    """`artifacts/dt=<date>/<session_id>/` — the folder the prompts and the manifest live in."""
    return f"{ARTIFACT_ROOT}/dt={partition_date(started_at)}/{_session_segment(session_id)}/"


def manifest_key(*, session_id: str, started_at: datetime) -> str:
    """The one object the CP itself writes. Under `artifacts/`, for the Athena reason at the top."""
    return artifact_prefix(session_id=session_id, started_at=started_at) + MANIFEST_NAME


#: The normalised event stream's folder under a session's artifact prefix (`app/session_events.py`). Under
#: `artifacts/`, not `transcripts/`: its lines are not transcript lines, so the Athena table never scans them.
EVENTS_FOLDER = "events"


def events_chunk_key(*, session_id: str, started_at: datetime, chunk: int) -> str:
    """`artifacts/dt=<date>/<session_id>/events/<chunk>.jsonl` — one CP-authored chunk of the event stream.

    Deterministic from the chunk index, so a reader derives every key from `sessions.events_last_seq` and never
    lists the bucket."""
    if not isinstance(chunk, int) or isinstance(chunk, bool) or chunk < 0:
        raise KeyRefused("event chunk index must be a non-negative int")
    return (
        artifact_prefix(session_id=session_id, started_at=started_at)
        + f"{EVENTS_FOLDER}/{chunk:010d}.jsonl"
    )


def member_key(*, session_id: str, started_at: datetime, name: str) -> str:
    """The object key one member gets. The only way a member's key is produced.

    Deterministic from the row's `started_at` and the allowlisted name, so a retry overwrites and
    leaves no orphans.
    """
    root = member_root(name)
    prefix = (
        transcript_prefix(session_id=session_id, started_at=started_at)
        if root == TRANSCRIPT_ROOT
        else artifact_prefix(session_id=session_id, started_at=started_at)
    )
    return prefix + name


def member_keys(
    *, session_id: str, started_at: datetime, names: Iterable[str]
) -> list[tuple[str, str]]:
    """`(name, key)` for each member, refusing the whole batch if any one name is not allowlisted.

    All-or-nothing, so the node never uploads a partial set and reports success.
    """
    resolved: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise KeyRefused(f"member {name!r} is named twice; each member gets one key")
        seen.add(name)
        resolved.append((name, member_key(session_id=session_id, started_at=started_at, name=name)))
    if not resolved:
        raise KeyRefused("a ticket must name at least one member")
    return resolved


def build_manifest(
    *,
    session_id: str,
    agent_kind: str,
    node: Optional[str],
    account: Optional[str],
    project: Optional[str],
    task_id: Optional[int],
    started_at: Optional[datetime],
    ended_at: Optional[datetime],
    end_reason: Optional[str],
    redaction: Optional[Mapping[str, Any]],
    members: Sequence[Mapping[str, Any]],
) -> bytes:
    """The `manifest.json` bytes the CP PUTs — the assembler's whole input.

    `bytes_raw` is load-bearing: `tarfile` writes the uncompressed size before the bytes and the
    assembler streams. `sha256` is the node's number over raw post-redaction pre-gzip bytes, copied
    (the CP sees only the gzip), so the assembler's check is a transport check, not a check on the node.

    Sorted keys and no ASCII escaping, so `transcript_sha256` over these bytes is reproducible.
    """
    document = {
        "schema": MANIFEST_SCHEMA,
        "session_id": session_id,
        "agent_kind": agent_kind,
        "node": node,
        "account": account,
        "project": project,
        "task_id": task_id,
        "started_at": utc_iso_or_none(started_at),
        "ended_at": utc_iso_or_none(ended_at),
        "end_reason": end_reason,
        # Absent redaction reporting is recorded as best-effort with nothing caught, never "clean".
        "redaction": dict(redaction) if redaction else {
            "patterns_version": 0, "patterns": [], "hits": 0,
            "confidence": REDACTION_CONFIDENCE,
        },
        "members": [
            {
                "name": member["name"],
                # Derived from the name, never taken from the node (it selects the key's root).
                "kind": member_kind(member["name"]),
                "bytes": int(member["bytes"]),
                "bytes_raw": int(member["bytes_raw"]),
                "sha256": member["sha256"],
            }
            for member in members
        ],
    }
    return json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")


def manifest_digest(manifest_bytes: bytes) -> str:
    """`sessions.transcript_sha256` — the sha256 of the bytes the CP itself wrote.

    Over the manifest, the only bytes the CP authors, so the one digest it computes rather than copies.
    """
    return hashlib.sha256(manifest_bytes).hexdigest()
