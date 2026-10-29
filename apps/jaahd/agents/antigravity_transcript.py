"""agents/antigravity_transcript.py — where `agy` keeps a session, and how the node finds and reads it.

Architecture §5.1's contract, delegated to by the runner adapter. Measured against agy 1.2.11 (linux amd64)
with a real signed-in session in a scratch HOME, one plain run and one that spawned a sub-agent
through `invoke_subagent`. Under `$HOME/.gemini/antigravity-cli/` the CLI writes:

  * `conversation_summaries.db` — SQLite (WAL mode, 0644): ONE row per conversation, keyed by the
    `conversation_id` the CLI mints itself (a UUID; `--help` has no flag to assign one, only `--conversation`
    to resume), with `workspace_uris` (a JSON list of `file://` URIs of the workspace's directories — EMPTY
    unless one is added with `--add-dir`, measured live on dev, so the runner always adds its
    worktree: `antigravity_runner.exec_line`) and
    `parent_conversation_id` ('' for a top-level session, the parent's id for a sub-agent). That is how a
    session is bound to its worktree, so it is the discovery index.
  * `brain/<id>/` — the session's own tree, which is `transcript_root`:
      - `.system_generated/logs/transcript_full.jsonl` — one JSON record per step (`step_index`, `type`,
        `created_at`, `content`, `tool_calls`), every tool result in FULL. The main record.
      - `.system_generated/logs/transcript.jsonl` — the same steps with long results cut (such a record
        carries `truncated_fields`: a 8330-char `seq` result was kept as 4120), so it is never the main
        record; `logs/chunks/{transcript,transcript_full}/<8 digits>.jsonl` are its chunked copies.
      - `.system_generated/steps/<n>/output.txt` — a tool's spilled output; `messages/` — the inter-agent
        mailbox; `subagents/<child id>.json` — one per sub-agent spawned (`{"conversationId": <child>}`),
        whose own tree is `brain/<child id>/`, laid out the same; `scratch/`, `.user_uploaded/`.
  * beside them, OUTSIDE `brain/`: `conversations/<id>.db` (the same steps as protobuf rows),
    `annotations/<id>.pbtxt` (the title) and `presence/<id>.lock` — next to the OAuth token file. They are
    deliberately NOT captured: the containment root must not hold the credential
    (`base.AgentAdapter.transcript_root`), and `transcript_full.jsonl` carries the same steps as text.

A turn that ends on an API error: the CLI writes its steps as `{"type": <the step type without its
`CORTEX_STEP_TYPE_` prefix>, "status": <the step status without `CORTEX_STEP_STATUS_`>, …}` (measured: USER_INPUT,
PLANNER_RESPONSE, GENERIC, SYSTEM_MESSAGE, all DONE), and its binary carries an `ERROR_MESSAGE` step type
(`HookInjectedStep.errorMessage` is the same step). `turn_error` reads a session that ENDS on such a step — the
CLI's own structured record, never prose — the shape inferred from those two facts, not yet seen written.

Usage: the session's files carry NO token counts. Per-step usage exists only in `--output-format
stream-json` stdout and in `conversations/<id>.db`'s protobuf step metadata, neither of which is a line
of the transcript this fold reads — so `read_usage` answers None (the `no-spend-reading` degradation).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
import urllib.parse
from datetime import datetime

from . import base

log = logging.getLogger(__name__)

# The session id is DISCOVERED after launch, never assigned: an assigned id makes the dispatcher append a
# `--session-id` agy does not accept (`fleet_dispatch.py`; agy 1.2.11 `--help` lists no such flag), and
# only DISCOVER is swept (`session_sweep.py`).
SESSION_BINDING: str = base.SESSION_DISCOVER

# The CLI's own ids are UUIDs; anything else is refused where it enters, before it becomes a path.
_ID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_STATE_DIR = os.path.join(".gemini", "antigravity-cli")
_INDEX_NAME = "conversation_summaries.db"
_INDEX_TABLE = "conversation_summaries"
# The index is ~1 KB per conversation (measured); a bound far above any real account, below outage scale.
_MAX_INDEX_BYTES = 64 * 1024 * 1024
# The session's main record and where its sub-agents are listed, relative to `brain/<id>/`.
_MAIN_RECORD = os.path.join(".system_generated", "logs", "transcript_full.jsonl")
_SUBAGENT_DIR = os.path.join(".system_generated", "subagents")
_HEAD_BYTES = 256 * 1024
_ROSTER_BYTES = 64 * 1024
# Directory levels walked below `brain/<id>/`: the deepest the CLI writes is 4 (`logs/chunks/transcript/`
# under `.system_generated`), and `session_sweep._VENDOR_MEMBER` names at most 6 directory segments, the
# id being the first.
_TREE_DEPTH = 5
# How deep sub-agents may nest before the rest are left out (and the listing says so).
_MAX_NESTING = 8
# How much of the main record's END `turn_error` reads: the closing step is the last record, and a turn's
# steps are written out of index order (step 2 before step 1, measured), so a few hundred KB holds the turn.
_TURN_TAIL_BYTES = 256 * 1024
# The CLI's error step (`CORTEX_STEP_TYPE_ERROR_MESSAGE`, agy 1.2.11), as the transcript spells a step type.
_ERROR_STEP = "ERROR_MESSAGE"
# The most of an error step's words handed on: the classifier reads a sentence, not a page.
_ERROR_TEXT_CHARS = 2000


class AntigravityLookupFailed(Exception):
    """THIS LOOKUP COULD NOT BE PERFORMED — as distinct from performing it and finding nothing.

    `session_sweep._vendor_transcript` reports None as the terminal `unavailable` and a raise as the
    retryable `failed`, so every "could not look" state raises."""


def _log(message: str) -> None:
    log.warning("antigravity transcript: %s", message)


def transcript_root(*, config_dir: str) -> str:
    """`<config_dir>/.gemini/antigravity-cli/brain` — every session's tree and nothing else.

    Narrower than the CLI's state dir on purpose: that dir also holds `antigravity-oauth-token`."""
    return os.path.join(config_dir, _STATE_DIR, "brain")


def _main_record(config_dir: str, session_id: str) -> str:
    return os.path.join(transcript_root(config_dir=config_dir), session_id, _MAIN_RECORD)


def _is_regular(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _created_at(path: str) -> float | None:
    """The epoch of the session's first step (`created_at` of its first record), or None."""
    head = base.read_head_lines(path, _HEAD_BYTES, 1)
    if not head:
        return None
    try:
        record = json.loads(head[0].decode("utf-8", "replace"))
        stamp = record.get("created_at") if isinstance(record, dict) else None
        return datetime.fromisoformat(stamp).timestamp() if isinstance(stamp, str) else None
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


def _workspace_paths(raw: object) -> set[str]:
    """`workspace_uris` (a JSON list of `file://` URIs) as resolved local paths."""
    try:
        uris = json.loads(raw) if isinstance(raw, str) else []
    except (ValueError, RecursionError):
        return set()
    paths = set()
    for uri in uris if isinstance(uris, list) else []:
        if isinstance(uri, str) and uri.startswith("file://"):
            paths.add(os.path.realpath(urllib.parse.unquote(urllib.parse.urlsplit(uri).path)))
    return paths


def _read_index(config_dir: str) -> list[tuple[str, set[str]]]:
    """Every TOP-LEVEL conversation in the index as `(id, workspace paths)`, most recently modified first.

    Read from a private copy (the file plus its `-wal`, each bounded): the CLI keeps the index in WAL
    mode, so a read-only open of the live file needs a writable `-shm` the daemon may not own, and a copy
    also means nothing this parser does can touch the runner's file. The copy is opened with SQLite's
    defensive settings — the file is runner-writable. `[]` when there is no index yet; raises when there
    is one that cannot be read."""
    path = os.path.join(config_dir, _STATE_DIR, _INDEX_NAME)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AntigravityLookupFailed(f"the conversation index {path!r} cannot be examined ({exc})") from exc
    index = base.read_bounded(path, _MAX_INDEX_BYTES)
    if not index:
        raise AntigravityLookupFailed(
            f"the conversation index {path!r} is not a readable regular file under "
            f"{_MAX_INDEX_BYTES} bytes")
    # A present `-wal` holds rows not yet checkpointed (a live session's own), so one that cannot be read is
    # a failure to look — never a quiet read of the main file alone, which would answer "no session".
    wal = b""
    try:
        wal_stat = os.lstat(path + "-wal")
    except FileNotFoundError:
        wal_stat = None
    except OSError as exc:
        raise AntigravityLookupFailed(f"the conversation index's log cannot be examined ({exc})") from exc
    if wal_stat is not None and not stat.S_ISREG(wal_stat.st_mode):
        raise AntigravityLookupFailed(f"the conversation index's log {path + '-wal'!r} is not a regular file")
    if wal_stat is not None and wal_stat.st_size:
        wal = base.read_bounded(path + "-wal", _MAX_INDEX_BYTES)
        if not wal:
            raise AntigravityLookupFailed(
                f"the conversation index's log {path + '-wal'!r} is not a readable regular file under "
                f"{_MAX_INDEX_BYTES} bytes")
    with tempfile.TemporaryDirectory(prefix="agy-index-") as scratch:
        copy = os.path.join(scratch, _INDEX_NAME)
        with open(copy, "wb") as handle:
            handle.write(index)
        if wal:
            with open(copy + "-wal", "wb") as handle:
                handle.write(wal)
        try:
            conn = sqlite3.connect(copy, timeout=0, isolation_level=None)
        except sqlite3.Error as exc:
            raise AntigravityLookupFailed(f"the conversation index copy cannot be opened ({exc})") from exc
        try:
            if hasattr(conn, "setconfig"):
                conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA cell_size_check=ON")
            kind = conn.execute("SELECT type FROM sqlite_master WHERE name = ?", (_INDEX_TABLE,)).fetchone()
            if kind is None:
                return []
            if kind[0] != "table":
                raise AntigravityLookupFailed(f"{_INDEX_TABLE!r} in {path!r} is a {kind[0]!r}, not a table")
            rows = conn.execute(
                f"SELECT conversation_id, workspace_uris FROM {_INDEX_TABLE} "
                "WHERE parent_conversation_id = '' ORDER BY last_modified_time DESC LIMIT ?",
                (base.MAX_TRANSCRIPT_SCAN + 1,)).fetchall()
        except sqlite3.Error as exc:
            raise AntigravityLookupFailed(f"the conversation index {path!r} cannot be read ({exc})") from exc
        finally:
            conn.close()
    if len(rows) > base.MAX_TRANSCRIPT_SCAN:
        raise AntigravityLookupFailed(
            f"the conversation index {path!r} holds more than {base.MAX_TRANSCRIPT_SCAN} sessions — "
            "refusing to name a session off a partial read")
    return [(str(cid), _workspace_paths(uris)) for cid, uris in rows
            if isinstance(cid, str) and _ID_RE.match(cid)]


def discover_session_id(*, config_dir: str, worktree: str, since: float) -> str | None:
    """The top-level session this CLI started in `worktree` at or after `since`, or None while there is none.

    The index names the worktree (`workspace_uris`); the session's own first step dates it, with a second
    of slack for the record's whole-second stamp. The EARLIEST match wins: every later one in the worktree
    is a successor's, and the sweep can run after the next attempt has started there (an ended attempt was
    once seen uploading its successor's session). Candidates are probed newest first and capped; since an
    unexamined older candidate could be the earliest, a cap that sheds some RAISES unless the probes
    already reached a session older than the launch (`base.AgentAdapter`)."""
    if not config_dir:
        return None
    rows = _read_index(config_dir)
    wanted = os.path.realpath(worktree) if worktree else ""
    matches = [cid for cid, paths in rows if not wanted or wanted in paths]
    best: tuple[float, str] | None = None
    reached_the_floor = False
    for cid in matches[:base.MAX_TRANSCRIPT_PROBES]:
        created = _created_at(_main_record(config_dir, cid))
        if created is None:
            continue
        if created + 1.0 < since:
            reached_the_floor = True
            continue
        if best is None or created < best[0]:
            best = (created, cid)
    if len(matches) > base.MAX_TRANSCRIPT_PROBES and not reached_the_floor:
        raise AntigravityLookupFailed(
            f"{len(matches)} sessions on this account ran in {worktree!r} and only the "
            f"{base.MAX_TRANSCRIPT_PROBES} most recent were examined, none older than the launch — "
            "refusing to name a session that an unexamined one could precede")
    return best[1] if best is not None else None


def transcript(*, config_dir: str, worktree: str, session_id: str) -> str | None:
    """`brain/<id>/.system_generated/logs/transcript_full.jsonl` — the untruncated record — or None."""
    del worktree
    if not (config_dir and session_id and _ID_RE.match(session_id)):
        return None
    path = _main_record(config_dir, session_id)
    return path if _is_regular(path) else None


def _mapped(rel: str) -> str | None:
    """`vendor/<rel>.gz`, each segment's leading dots dropped (`session_sweep._VENDOR_MEMBER` wants every
    segment to start with a letter or digit, and the CLI's own dirs are `.system_generated` and
    `.user_uploaded`); None for a path no member name can carry."""
    parts = [part.lstrip(".") for part in rel.split(os.sep)]
    if not all(parts) or len(parts) > _TREE_DEPTH + 2:
        return None
    return "vendor/" + "/".join(parts) + ".gz"


def _subagents(root: str, session_id: str) -> list[str]:
    """The sub-agent ids `brain/<id>/.system_generated/subagents/` lists, each gated on its roster file
    naming itself (`conversationId`) — a planted file under the name lists nothing."""
    directory = os.path.join(root, session_id, _SUBAGENT_DIR)
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return []
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    children = []
    # Bounded like any probe: a runner chooses how many roster files there are.
    for name in names[:base.MAX_TRANSCRIPT_PROBES]:
        child = name[:-len(".json")] if name.endswith(".json") else ""
        if not _ID_RE.match(child):
            continue
        try:
            roster = json.loads(base.read_bounded(os.path.join(directory, name), _ROSTER_BYTES) or b"null")
        except (ValueError, RecursionError):
            continue
        if isinstance(roster, dict) and roster.get("conversationId") == child:
            children.append(child)
    return children


def session_members(*, config_dir: str, transcript: str) -> list[tuple[str, str]]:
    """Every other file of the session whose main record is `transcript`, as `(member name, path)`.

      * each sub-agent's own `transcript_full.jsonl` -> `subagent-<child id>.jsonl.gz`, found through the
        parent's `subagents/` roster and recursively through each child's (nested delegation);
      * every other file under `brain/<id>/` and under each sub-agent's `brain/<child id>/` — the truncated
        `transcript.jsonl`, the chunked copies, spilled tool outputs, the mailbox, the roster, scratch and
        uploads -> `vendor/<path under brain/, leading dots dropped>.gz`.

    Raises `base.IncompleteSessionMembers`, carrying every member it did find, when a walk hit its budget,
    a directory could not be listed, or a tree was deeper than a member name can carry."""
    if not (config_dir and transcript):
        return []
    root = os.path.realpath(transcript_root(config_dir=config_dir))
    main = os.path.realpath(transcript)
    rel = os.path.relpath(main, root).split(os.sep)
    if len(rel) < 2 or not _ID_RE.match(rel[0]) or os.path.join(*rel[1:]) != _MAIN_RECORD:
        return []
    # `deep_is_short`: a tree past `_TREE_DEPTH` has no member name (`_mapped`), so it is a shortfall.
    walk = base.SessionTreeWalk(root, deep_is_short=True)
    members: list[tuple[str, str]] = []
    queue: list[tuple[str, int]] = [(rel[0], 0)]
    seen = {rel[0]}
    while queue:
        session_id, nesting = queue.pop(0)
        record = os.path.join(root, session_id, _MAIN_RECORD)
        for path in walk.files(os.path.join(root, session_id), max_depth=_TREE_DEPTH + 1):
            if path == record:
                continue
            name = _mapped(os.path.relpath(path, root))
            if name is None:
                walk.shortfall.append(f"{path!r} has no member name")
                continue
            members.append((name, path))
        for child in _subagents(root, session_id):
            if child in seen:
                continue
            seen.add(child)
            if nesting + 1 > _MAX_NESTING:
                walk.shortfall.append(f"sub-agent {child} is nested more than {_MAX_NESTING} deep")
                continue
            try:
                # A real directory only: `scandir` would follow a planted link at the top of the child's tree.
                if not stat.S_ISDIR(os.lstat(os.path.join(root, child)).st_mode):
                    continue
            except OSError:
                continue
            child_record = os.path.join(root, child, _MAIN_RECORD)
            if _is_regular(child_record):
                members.append((f"subagent-{child}.jsonl.gz", child_record))
            queue.append((child, nesting + 1))
    if walk.shortfall:
        _log(f"session {rel[0]}'s files were not all listed: {walk.shortfall[0]}"
             + (f" (and {len(walk.shortfall) - 1} more)" if len(walk.shortfall) > 1 else ""))
        raise base.IncompleteSessionMembers(
            f"antigravity session {rel[0]}: {walk.shortfall[0]}", members)
    return members


def read_usage(line: bytes) -> base.TokenCounts | None:
    """Always None: no transcript line carries token counts (module docstring; `no-spend-reading`)."""
    del line
    return None


def _step_index(record: dict) -> int | None:
    value = record.get("step_index")
    try:
        return int(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else None
    except ValueError:
        return None


def turn_error(*, transcript: str) -> base.TurnError | None:
    """The error the session's last turn ended on, or None — `AgentAdapter.turn_error` (module docstring).

    The highest-indexed step within the tail decides: only an `ERROR_MESSAGE` step there counts, so an error the
    CLI recovered from (a later step follows it) and a runner merely writing about errors are never read as a
    break. The step index names the turn (`marker`)."""
    last: tuple[int, dict] | None = None
    for raw in base.read_tail_lines(transcript, _TURN_TAIL_BYTES) if transcript else []:
        record = base.json_object(raw)
        index = _step_index(record) if record is not None else None
        if record is not None and index is not None and (last is None or index >= last[0]):
            last = (index, record)
    if last is None or last[1].get("type") != _ERROR_STEP:
        return None
    text = next((v.strip() for v in (last[1].get("content"), last[1].get("error"))
                 if isinstance(v, str) and v.strip()), "")
    if not text:
        return None
    return base.TurnError(marker=f"step-{last[0]}", text=text[:_ERROR_TEXT_CHARS])
