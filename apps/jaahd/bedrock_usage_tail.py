"""`bedrock-usage-tail` — the live half of Jaah token metering.

Every 10 s, for each live runner or console tab this node launched on the Jaah account, read the new bytes
of its Claude transcript(s) — the session's own file and its `subagents/` — from a persisted byte offset,
fold each model call with `session_sweep.Analytics` (the single home of the billing fold), key it by K5
(`bedrock_call_key`) and post it to K4, `POST /api/agent/jaah-sessions/{uuid}/usage`, at most `MAX_BATCH`
per post. The CP debits each call individually and is idempotent on its key; AWS's invocation log corrects
the node afterwards. The end-of-session sweep (`session_sweep`) is a separate, unchanged path.

WHEN A CALL IS POSTED. Claude Code writes an assistant response once it is complete — one line per content
block, each repeating the response's final `usage` and `stop_reason` (measured). A call is therefore posted
as soon as a line of it carries a `stop_reason`, or a later user/assistant line follows it, or its file has
been quiet for `IDLE_FINAL_S`, or its session has ended. Nothing else is held back.

NO LOSS, NO REPEAT, ACROSS RESTARTS. Per file the record holds the byte offset below which every call is
posted, and a ring of the `(message.id, requestId)` pairs posted most recently. The offset only moves after
the CP accepted the calls behind it, so a refused or failed post (an older CP answering 404 for K4, a
network fault) is retried, never dropped; the ring makes a response whose lines straddle a read count once.

ACCEPTED RESIDUAL: a console tab is tailed under the Claude session id it opened with; a human's `/clear` or
`/resume` moves it to another file, metered only by AWS's invocation log (I1). A node report is only as
honest as a transcript the runner can write — the implausibility bound keeps a forged line from being
debited live, and AWS's log is what settles every call.

STATE lives in the DAEMON's own tree (`env_helpers.daemon_state_dir`), one file per Jaah session, written by
the launch path (`register`) — never in the session user's tree, where a runner could rewrite its offsets.
A record outlives its pane until its last calls are posted, then goes; past `DEAD_HORIZON_S` it is dropped
loudly (the CP no longer takes those calls; the AWS correction still bills them).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import bedrock_call_key
import bedrock_launch
import control_plane_api
import env_helpers
import file_io
import host_platform

log = logging.getLogger("jaahd.bedrock_usage_tail")

INTERVAL_SECONDS = 10.0
#: K4's per-post ceiling.
MAX_BATCH = 200
#: Bytes read from one file per tick: a transcript that grew faster drains over the next ticks.
MAX_READ_BYTES = 4 * 1024 * 1024
#: Recently posted `(message.id, requestId)` pairs remembered per file.
POSTED_RING = 512
#: A trailing call with no `stop_reason` is posted once its file has been quiet this long.
IDLE_FINAL_S = 30.0
#: After a failed post, the session's next attempt waits this long.
RETRY_BACKOFF_S = 60.0
#: How long after its pane ended a record keeps retrying before it is dropped.
DEAD_HORIZON_S = 24 * 3600.0

_STATE_PARTS = ("bedrock-usage",)
_STATE_DIR_MODE = 0o700
# Line types that close the response before them; everything else (attachments, titles, modes) does not.
_TURN_TYPES = frozenset({"user", "assistant"})


def state_dir() -> Path:
    return env_helpers.daemon_state_dir(*_STATE_PARTS)


def _record_path(jaah_session_id: str) -> Path:
    return state_dir() / f"{uuid.UUID(jaah_session_id)}.json"


def _write_record(record: dict) -> None:
    """Atomic replace, `0600` in a `0700` dir the daemon owns."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, _STATE_DIR_MODE)
    env_helpers.atomic_write_json(_record_path(record["jaah_session_id"]), record, mode=0o600)


def register(*, jaah_session_id: str, pane_pid: object, config_dir: str, worktree: str,
             native_session_id: str) -> bool:
    """Start tailing one Jaah session: the pane it runs in and where its transcript will be. Called by the
    launch path the moment the pane pid is known, or — on the ACP runtime — by the hub once the host reports the
    agent's own session id (`acp/runtime.py`). Idempotent: a session already tailed on the same pane keeps its
    record, so no offset is reset and no call posted twice. Best effort: a fault is logged and returns False —
    the session still runs, and AWS's invocation log still bills it."""
    try:
        sid = str(uuid.UUID(str(jaah_session_id)))
        pid = int(str(pane_pid).strip())
    except (TypeError, ValueError):
        log.warning("bedrock-usage-tail: not tailing session %r on pane %r — not a session id and pid",
                    jaah_session_id, pane_pid)
        return False
    ticks = host_platform.start_time(pid)
    if ticks is None or not config_dir or not worktree or not native_session_id:
        log.warning("bedrock-usage-tail: not tailing Jaah session %s — its pane %s is gone or its "
                    "transcript cannot be located", sid, pid)
        return False
    current = file_io.read_json_or(_record_path(sid), {}, expect=dict)
    if (current.get("pane_pid"), current.get("pane_start_ticks"),
            current.get("claude_session_id")) == (pid, ticks, native_session_id):
        return True
    try:
        # The record keeps its `claude_session_id` key: on-disk state a tail already running under an older
        # daemon wrote, read on every tick, so the keyword's rename stops at this line.
        _write_record({
            "jaah_session_id": sid, "pane_pid": pid, "pane_start_ticks": ticks, "config_dir": config_dir,
            "worktree": worktree, "claude_session_id": native_session_id, "registered_at": time.time(),
            "dead_at": None, "files": {},
        })
    except OSError as exc:
        log.warning("bedrock-usage-tail: could not record Jaah session %s (%s) — its calls are billed only "
                    "by AWS's invocation log", sid, exc)
        return False
    log.info("bedrock-usage-tail: tailing Jaah session %s (pane %s)", sid, pid)
    return True


def _load_records() -> list[dict]:
    directory = state_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    records = []
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        record = file_io.read_json_or(directory / name, {}, expect=dict)
        try:
            uuid.UUID(str(record.get("jaah_session_id")))
        except ValueError:
            log.warning("bedrock-usage-tail: skipping an unreadable record %s", name)
            continue
        records.append(record)
    return records


# ── The fold: bytes → final calls ─────────────────────────────────────────────────────────────────────

@dataclass
class _Group:
    """One response: its lines, the byte offset of its first line, and whether it is known complete."""

    key: tuple
    start: int
    lines: list = field(default_factory=list)
    message_id: Optional[str] = None
    request_id: Optional[str] = None
    timestamp: str = ""
    stopped: bool = False
    final: bool = False


@dataclass(frozen=True)
class Call:
    """One call to post, with the offset of its first line (where a retry must re-read from)."""

    start: int
    pair: str
    body: dict


def _pair(key: tuple) -> str:
    return f"{key[0]}|{key[1]}"


def _occurred_at(stamp: str) -> Optional[datetime]:
    """The line's own timestamp, or None. Never "now": K5's fallback hashes it, so a key that changed on a
    retry would bill an accepted-but-unacknowledged call twice."""
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def scan(data: bytes, base: int, *, session_complete: bool, idle: bool) -> tuple[list[_Group], int]:
    """`(the complete responses in `data`, the offset below which all of them lie)`.

    `data` is the file from byte `base`; only whole lines are read. A response is complete when a line of
    it carries a `stop_reason`, a later user/assistant line follows it, or — for the trailing one —
    `session_complete` or `idle`. The returned offset is the first line of the trailing incomplete
    response, else the end of the last whole line."""
    groups: list[_Group] = []
    current: Optional[_Group] = None
    position = 0
    while True:
        end = data.find(b"\n", position)
        if end < 0:
            break
        raw = data[position:end]
        offset = base + position
        position = end + 1
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if obj.get("type") == "assistant" and isinstance(usage, dict):
            message_id = message.get("id")
            key = (message_id, obj.get("requestId")) if message_id is not None else (None, f"@{offset}")
            if current is not None and current.key == key:
                current.lines.append(raw)
            else:
                if current is not None:
                    current.final = True
                current = _Group(key=key, start=offset, lines=[raw],
                                 message_id=str(message_id) if message_id is not None else None,
                                 request_id=str(obj["requestId"]) if obj.get("requestId") else None,
                                 timestamp=str(obj.get("timestamp") or ""))
                groups.append(current)
            if message.get("stop_reason"):
                current.stopped = True
        elif obj.get("type") in _TURN_TYPES and current is not None:
            current.final = True
            current = None
    if current is not None and (current.stopped or session_complete or idle):
        current.final = True
    pending = next((g for g in groups if not g.final), None)
    return [g for g in groups if g.final], (pending.start if pending is not None else base + position)


#: Usage no real call reports — far past any model's context and output ceilings. Such a line was forged
#: or corrupted; it is left to AWS's invocation log, the source of truth, rather than debited on our say-so.
MAX_PLAUSIBLE_INPUT = 2_000_000
MAX_PLAUSIBLE_OUTPUT = 256_000


def calls_for(groups: list[_Group], *, posted: set[str], role_session_name: str,
              sent: Optional[dict] = None) -> list[Call]:
    """The K4 call for each complete response not already posted and not zero-spend, at most once per
    `(message.id, requestId)` even when one response's lines are split across a read. A pair in `sent` (a
    post whose answer never came back) reuses the body sent then, so a retry can never re-key it."""
    import session_sweep  # noqa: PLC0415 — the billing fold's single home; heavy, so lazy

    sent = sent or {}
    seen = set(posted)
    out: list[Call] = []
    for group in groups:
        pair = _pair(group.key)
        if pair in seen:
            continue
        seen.add(pair)
        if pair in sent:
            out.append(Call(start=group.start, pair=pair, body=dict(sent[pair])))
            continue
        fold = session_sweep.Analytics()
        for line in group.lines:
            fold.feed(line)
        by_model = fold.usage_by_model()
        if not by_model:
            continue
        model, tokens = max(by_model.items(), key=lambda kv: sum(kv[1].values()))
        if tokens["output"] > MAX_PLAUSIBLE_OUTPUT or \
                sum(v for cls, v in tokens.items() if cls != "output") > MAX_PLAUSIBLE_INPUT:
            log.warning("bedrock-usage-tail: implausible usage on message %r — left to the AWS correction",
                        group.message_id)
            continue
        occurred = _occurred_at(group.timestamp)
        if occurred is None:
            # Claude stamps every line; one without a stamp cannot be keyed stably, so it is left to AWS's
            # invocation log (I1), which bills it exactly once.
            log.warning("bedrock-usage-tail: a call with no timestamp (message %r) is left to the AWS "
                        "correction", group.message_id)
            continue
        key = bedrock_call_key.call_key(
            request_id=group.request_id, role_session_name=role_session_name, occurred_at=occurred,
            output_tokens=tokens["output"], use_request_id=bedrock_call_key.USE_REQUEST_ID)
        out.append(Call(start=group.start, pair=pair, body={
            "call_key": key, "request_id": group.request_id, "message_id": group.message_id,
            "model": model, "occurred_at": occurred.astimezone(timezone.utc).isoformat(),
            "tokens": dict(tokens),
        }))
    return out


# ── One tick ──────────────────────────────────────────────────────────────────────────────────────────

#: Per session per tick: a runner can write its own transcript, so one session may not stall the others'
#: metering or flood the CP. What is over the budget is read next tick, never dropped.
MAX_SUBAGENT_FILES = 256
MAX_CALLS_PER_TICK = 4 * MAX_BATCH
MAX_BYTES_PER_TICK = 4 * MAX_READ_BYTES
#: A refusal no retry can change: the call is malformed. Logged loudly and skipped (AWS's log still bills
#: it), so one bad body never freezes the session's meter.
_PERMANENT_REFUSALS = frozenset({400, 422})

DRAINED, MORE, FAILED = "drained", "more", "failed"


def _transcripts(record: dict) -> list[Path]:
    """The session's own transcript and its subagents', each contained under the config dir's projects."""
    import session_sweep  # noqa: PLC0415

    main = session_sweep.main_transcript(record["config_dir"], record["worktree"], record["claude_session_id"])
    root = Path(record["config_dir"]) / "projects"
    if root.is_symlink():
        # The root's parent is the session user's: a link there would make "contained" mean anything.
        log.warning("bedrock-usage-tail: %s is a symlink — not reading Jaah session %s's transcripts", root,
                    record["jaah_session_id"])
        return []
    found = session_sweep.contained_file(main, root) if main.exists() else None
    if found is None:
        return []
    subagents = [path for _name, path in session_sweep.subagent_members(found, record["claude_session_id"])]
    if len(subagents) > MAX_SUBAGENT_FILES:
        log.warning("bedrock-usage-tail: Jaah session %s has %d subagent transcripts — metering the first %d "
                    "live; AWS's invocation log bills the rest", record["jaah_session_id"], len(subagents),
                    MAX_SUBAGENT_FILES)
    return [found, *subagents[:MAX_SUBAGENT_FILES]]


class Tail:
    """The `bedrock-usage-tail` step. In-memory state is only latency bookkeeping (idle detection and
    backoff); everything that decides what has been posted is on disk."""

    def __init__(self, *, post: Optional[Callable[[str, list], dict]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._post = post or control_plane_api.post_jaah_session_usage
        self._clock = clock
        self._last_size: dict[str, tuple[int, float]] = {}
        self._next_attempt: dict[str, float] = {}

    async def step(self) -> None:
        await asyncio.to_thread(self.tick)

    def tick(self) -> None:
        for record in _load_records():
            try:
                self._session(record)
            except Exception:  # noqa: BLE001 — one session's fault never costs the others their tick
                log.warning("bedrock-usage-tail: Jaah session %s failed this tick",
                            record.get("jaah_session_id"), exc_info=True)

    def _alive(self, record: dict) -> bool:
        ticks = host_platform.start_time(int(record["pane_pid"]))
        return ticks is not None and ticks == record.get("pane_start_ticks")

    def _session(self, record: dict) -> None:
        sid = record["jaah_session_id"]
        now = self._clock()
        if record.get("dead_at") is None and not self._alive(record):
            record["dead_at"] = now
            _write_record(record)
        dead_at = record.get("dead_at")
        if dead_at is not None and now - dead_at > DEAD_HORIZON_S:
            log.warning("bedrock-usage-tail: dropping Jaah session %s — its pane ended %.0fh ago and its "
                        "last calls were never accepted; AWS's invocation log bills them", sid,
                        (now - dead_at) / 3600)
            self._forget(record)
            return
        if self._next_attempt.get(sid, 0.0) > now:
            return
        budget = {"calls": MAX_CALLS_PER_TICK, "bytes": MAX_BYTES_PER_TICK}
        paths = _transcripts(record)
        outcome = DRAINED
        for path in paths:
            outcome = self._file(record, path, now, session_complete=dead_at is not None, budget=budget)
            if outcome != DRAINED:
                break
        if outcome == FAILED:
            self._next_attempt[sid] = now + RETRY_BACKOFF_S
            return
        self._next_attempt.pop(sid, None)
        if outcome == DRAINED and dead_at is not None:
            if paths:
                log.info("bedrock-usage-tail: Jaah session %s ended and every call is posted", sid)
            else:
                log.warning("bedrock-usage-tail: Jaah session %s ended with no transcript found — nothing "
                            "metered live; AWS's invocation log bills it", sid)
            self._forget(record)

    def _forget(self, record: dict) -> None:
        sid = record["jaah_session_id"]
        try:
            _record_path(sid).unlink()
        except FileNotFoundError:
            pass
        self._next_attempt.pop(sid, None)
        for path in record.get("files", {}):
            self._last_size.pop(path, None)

    def _file(self, record: dict, path: Path, now: float, *, session_complete: bool, budget: dict) -> str:
        """Post this file's new calls, chunk after chunk, until it is drained, the tick's budget is spent
        (`MORE`), or a post fails (`FAILED`: the offset stays at the first call not accepted)."""
        import session_sweep  # noqa: PLC0415

        state = record["files"].setdefault(str(path), {"offset": 0, "posted": []})
        try:
            size = path.stat().st_size
        except OSError:
            return DRAINED
        if size < state["offset"]:
            # Rewritten under us: start over; the ring and the CP's idempotency keep it from double-billing.
            log.warning("bedrock-usage-tail: %s shrank below its offset — re-reading it from the start", path)
            state["offset"] = 0
        last = self._last_size.get(str(path))
        if last is None or last[0] != size:
            self._last_size[str(path)] = (size, now)
            idle = False
        else:
            idle = now - last[1] >= IDLE_FINAL_S
        role = bedrock_launch.role_session_name(record["jaah_session_id"])
        while state["offset"] < size:
            if budget["calls"] <= 0 or budget["bytes"] <= 0:
                return MORE
            try:
                handle = session_sweep.open_no_follow(path)
            except OSError as exc:
                log.warning("bedrock-usage-tail: cannot open %s (%s) — skipped this tick", path, exc)
                return DRAINED
            with handle:
                handle.seek(state["offset"])
                data = handle.read(MAX_READ_BYTES)
                if len(data) == MAX_READ_BYTES and b"\n" not in data:
                    # One line longer than a read (a large tool result): no call is billed on it, so step
                    # over it rather than re-read it forever.
                    skipped = self._skip_line(handle, state["offset"] + len(data))
                    budget["bytes"] -= skipped - state["offset"]
                    log.warning("bedrock-usage-tail: %s: skipped a %d-byte line", path, skipped - state["offset"])
                    state["offset"] = skipped
                    _write_record(record)
                    continue
            budget["bytes"] -= len(data)
            start = state["offset"]
            groups, new_offset = scan(data, start, session_complete=session_complete,
                                      idle=idle and len(data) == size - start)
            outcome = self._post_calls(record, state, groups, new_offset, role, budget)
            if outcome != DRAINED:
                return outcome
            if new_offset == start:
                # Only a trailing response that is not complete yet (or a partial last line): wait for it.
                return DRAINED
        return DRAINED

    @staticmethod
    def _skip_line(handle, position: int) -> int:
        """The offset just past the next newline at or after `position`, or the end of the file."""
        handle.seek(position)
        while True:
            chunk = handle.read(MAX_READ_BYTES)
            if not chunk:
                return position
            found = chunk.find(b"\n")
            if found >= 0:
                return position + found + 1
            position += len(chunk)

    def _post_calls(self, record: dict, state: dict, groups: list, new_offset: int, role: str,
                    budget: dict) -> str:
        sid = record["jaah_session_id"]
        posted = list(state["posted"])
        sent = dict(state.get("sent") or {})
        calls = calls_for(groups, posted=set(posted), role_session_name=role, sent=sent)
        for index in range(0, len(calls), MAX_BATCH):
            batch = calls[index:index + MAX_BATCH]
            try:
                answer = self._post(sid, [c.body for c in batch])
            except Exception as exc:  # noqa: BLE001 — classified below; never silently dropped
                code = getattr(exc, "code", None)
                if code in _PERMANENT_REFUSALS:
                    log.error("bedrock-usage-tail: Jaah session %s: the CP refused %d call(s) as malformed "
                              "(HTTP %s) — skipped; AWS's invocation log bills them", sid, len(batch), code)
                    answer = {}
                else:
                    log.warning("bedrock-usage-tail: Jaah session %s: %d call(s) not accepted (%s) — retrying "
                                "from the same offset", sid, len(batch),
                                f"HTTP {code}" if code else type(exc).__name__)
                    # Everything before this batch was accepted: resume at its first call, with the very
                    # bodies sent now — an answer lost in flight must not come back under a new key.
                    state["offset"] = batch[0].start
                    state["posted"] = posted[-POSTED_RING:]
                    state["sent"] = {**sent, **{c.pair: c.body for c in batch}}
                    _write_record(record)
                    return FAILED
            else:
                log.info("bedrock-usage-tail: Jaah session %s: posted %d call(s) (accepted %s, duplicates %s)",
                         sid, len(batch), answer.get("accepted"), answer.get("duplicates"))
            posted.extend(c.pair for c in batch)
            for call in batch:
                sent.pop(call.pair, None)
            budget["calls"] -= len(batch)
        state["offset"] = new_offset
        state["posted"] = posted[-POSTED_RING:]
        state["sent"] = sent
        _write_record(record)
        return DRAINED
