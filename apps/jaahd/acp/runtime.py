"""The daemon's half of the ACP host runtime: the event log, the control lane and open user-input requests
of every ACP session this node runs (architecture §3.1-§3.3).

The host (`acp/host.py`, one per run, inside the runner's pane) reaches this hub over the agent socket with
five TASK-scoped daemon-local verbs (`agent_vocabulary.LOCAL_VERBS`). IDENTITY IS THE PEER'S: `identity` is
`runner_registry`'s (or a VM's) answer for the caller, never a value from `args`, and every verb is fenced
to that identity's own session — a caller naming another session is refused, so runner A can neither write
runner B's log, read B's operator intents, nor collect B's answers.

  * `runtime.events_append` — the daemon owns `seq` (per session, persisted, monotonic) and stamps
    `session_id`/`task_id` from the identity; a replayed `batch_id` is answered with its first seqs and
    appends nothing, so the host may retry a batch after an `unavailable`. Appended to a JSONL file, then
    forwarded to the CP by ONE forwarder thread from a persisted cursor (idempotent on seq CP-side).
  * `runtime.control_wait` / `runtime.request_wait` — long polls PARKED ON THE LOOP (the broker's wait
    lane), resolved by `enqueue_control` / `resolve_request`, which reconcile calls when the CP sends an
    operator's steer/interrupt/answer/close. A decided request is kept `DECIDED_GRACE_S` after delivery.
  * `runtime.request_open` / `runtime.request_cancel` — the host opens a user-input request, and withdraws
    it when the agent does.
  * `runtime.account_key` — the key of the runner's leased API-key account, once per session, for a launch that
    names `authenticate` (`account_key`); the host sends it in ACP `authenticate` and drops it.

Every verb serves only a session a launcher recorded (`acp_runtime.register_session`) for the caller's task; any
other is refused, never created. Only the run's HOST may call these: `cred_broker` refuses a pid caller that is not its pane's root process.

Event payloads and answer text are an operator's or a model's words: never logged here. Stdlib only at module scope (a Jaah-account
session's metering is imported where it is used)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import agent_vocabulary
from acp.session_store import (O_BINARY, O_CLOEXEC, O_NOFOLLOW, SessionStore, private_dir, read_json,
                               valid_session_id, write_json_private, write_text_private)

log = logging.getLogger("jaahd.acp.runtime")

MAX_WAIT_S = 50.0
INTENT_RETENTION_S = 24 * 3600
MAX_EVENTS_PER_BATCH = 512
INTENT_TYPES = ("steer", "interrupt", "answer", "close")
# One serialized event, stamped. Below the socket's 256 KiB request line with room for the envelope: the
# host splits a longer item into chunks of the same item_id, each of which fits.
MAX_EVENT_BYTES = 192 * 1024
# The CP post size, in events and in bytes (whichever binds first; one event always goes).
MAX_FORWARD_EVENTS = 500
MAX_FORWARD_BYTES = 1024 * 1024
# A session's events file may grow to this; past it `events_append` answers `busy` (logged once).
MAX_SESSION_EVENT_BYTES = 256 * 1024 * 1024
# Batch ids remembered per session for dedup.
MAX_BATCH_IDS = 256
# Open (unresolved or undelivered) requests per session, and queued intents: a host cannot grow either
# without bound, nor can a CP that re-sends.
MAX_OPEN_REQUESTS = 16
# Every entry, delivered ones in their grace period included: an open/cancel loop cannot grow the table.
MAX_HELD_REQUESTS = 64
MAX_QUEUED_INTENTS = 256
# An intent's own payload, serialized (a steer text or a composed answer).
MAX_INTENT_BYTES = 256 * 1024
# A decided request is kept this long after its first delivery, so a wait retried after a lost reply reads
# the same decision; an undecided one is dropped after a day.
DECIDED_GRACE_S = 120.0
REQUEST_RETENTION_S = 24 * 3600
# Answer intent ids remembered per session, so a re-sent `deliver_answer` changes nothing.
MAX_ANSWER_INTENTS = 256
# In-memory state of a session idle this long is evicted; its files are deleted once untouched for a week.
# The sweep that does both runs at most hourly (`maybe_sweep`, from `hub()`).
SESSION_IDLE_EVICT_S = 24 * 3600
SESSION_FILE_RETENTION_S = 7 * 24 * 3600
SWEEP_INTERVAL_S = 3600.0
# The forwarder's own retry after a failed post, doubling to the cap.
FORWARD_BACKOFF_S = 5.0
FORWARD_BACKOFF_MAX_S = 300.0
_MAX_ID_CHARS = 128
# A seq recovered from the events file's tail reads at most this much (one event is far below it).
_SEQ_TAIL_BYTES = 256 * 1024
# Every file a session owns, by suffix (the session id may itself contain dots).
_SUFFIXES = ("session.json", "seq", "batches.json", "control.json", "requests.json", "answered.json",
             "events.jsonl", "fwd")

#: The hub's directory under the daemon state dir; the session sweep reads a manifest 2 run's events there.
RUNS_DIR = "acp-runs"

Forward = Callable[[str, str, list], None]


def _refuse(reason: str, detail: str) -> agent_vocabulary.VerbRefused:
    return agent_vocabulary.VerbRefused(reason, detail)


def _bad(detail: str) -> agent_vocabulary.VerbRefused:
    return _refuse(agent_vocabulary.ERR_BAD_REQUEST, detail)


def _short_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_ID_CHARS


@dataclass
class _Session:
    """One session's in-memory state, loaded from its files on first touch. `seq`/`batches`/`events_bytes`
    are mutated only under `append_lock`; the rest only under the hub lock, which is never held across file
    I/O (the broker's event loop takes it)."""

    seq: int = 0
    batches: list = field(default_factory=list)          # [[batch_id, first_seq, last_seq], ...]
    events_bytes: int = 0
    budget_logged: bool = False
    control: dict = field(default_factory=lambda: {"next": 0, "intents": []})
    requests: dict = field(default_factory=dict)         # request_id -> {request, opened_at, decision, answers}
    answered: list = field(default_factory=list)         # answer intent ids already applied
    control_waiters: list = field(default_factory=list)  # [(loop, future)]
    request_waiters: dict = field(default_factory=dict)  # request_id -> [(loop, future)]
    touched: float = 0.0
    append_lock: threading.Lock = field(default_factory=threading.Lock)


def _wake(waiters: list) -> None:
    """Resolve each parked future on its own loop. Called under the hub lock, possibly off-loop."""
    for loop, fut in waiters:
        try:
            loop.call_soon_threadsafe(_set_done, fut)
        except RuntimeError:
            pass  # that loop is closed: its waiter is gone with it


def _set_done(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)


def _open_append(path: Path) -> int:
    """An append fd on `path`, created 0600, never through a symlink (a planted link is ELOOP, not a write
    to wherever it points)."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_BINARY, 0o600)
    if hasattr(os, "fchmod"):
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
    return fd


def _last_seq(path: Path) -> int:
    """The highest seq on the events file's last complete lines, or 0 — the counter's fallback when its own
    file is missing or torn."""
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_BINARY)
    except OSError:
        return 0
    with os.fdopen(fd, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - _SEQ_TAIL_BYTES))
        tail = fh.read()
    best = 0
    for line in tail.split(b"\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("seq"), int):
            best = max(best, event["seq"])
    return best


def _file_size(path: Path) -> int:
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


class RuntimeHub:
    """Thread-safe: `_lock` guards the in-memory session table (O(1) critical sections, safe on the broker's
    loop), each session's `append_lock` its event log, `_io_lock` the order of control/request file writes,
    and `_fwd_io_lock` the forwarder's cursor."""

    def __init__(self, root: Path, *, forward: Forward | None = None, clock: Callable[[], float] = time.time):
        self.root = private_dir(Path(root))
        self.store = SessionStore(self.root)
        self._forward = forward
        self._clock = clock
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._fwd_lock = threading.Lock()
        self._fwd_io_lock = threading.Lock()
        self._fwd_queue: "queue.Queue[str]" = queue.Queue()
        self._fwd_pending: set[str] = set()
        self._fwd_retry: dict[str, tuple[float, float]] = {}  # session -> (monotonic due, backoff)
        self._fwd_thread: threading.Thread | None = None
        self._sweep_lock = threading.Lock()
        self._last_sweep = float("-inf")

    # ── files ───────────────────────────────────────────────────────────────────────────────────────────

    def _file(self, session_id: str, suffix: str) -> Path:
        return self.root / f"{session_id}.{suffix}"

    def _load(self, session_id: str) -> _Session:
        """The session's state as its files hold it. File I/O, no lock: never on the loop."""
        st = _Session()
        events = self._file(session_id, "events.jsonl")
        seq = read_json(self._file(session_id, "seq"))
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            st.seq = seq
        else:  # missing or torn: never reuse a seq the log already carries
            st.seq = _last_seq(events)
        st.events_bytes = _file_size(events)
        batches = read_json(self._file(session_id, "batches.json"))
        if isinstance(batches, list):
            st.batches = [b for b in batches if isinstance(b, list) and len(b) == 3][-MAX_BATCH_IDS:]
        control = read_json(self._file(session_id, "control.json"))
        if isinstance(control, dict) and isinstance(control.get("next"), int) and isinstance(
                control.get("intents"), list):
            st.control = {"next": control["next"],
                          "intents": [i for i in control["intents"] if isinstance(i, dict)]}
        requests = read_json(self._file(session_id, "requests.json"))
        if isinstance(requests, dict):
            st.requests = {k: v for k, v in requests.items() if isinstance(v, dict)}
        answered = read_json(self._file(session_id, "answered.json"))
        if isinstance(answered, list):
            st.answered = [a for a in answered if isinstance(a, str)][-MAX_ANSWER_INTENTS:]
        return st

    def _ensure(self, session_id: str) -> _Session:
        """The session's state, loaded once (off the lock). Synchronous: an executor thread's call."""
        with self._lock:
            st = self._sessions.get(session_id)
            if st is not None:
                st.touched = self._clock()
                return st
        loaded = self._load(session_id)
        with self._lock:
            st = self._sessions.setdefault(session_id, loaded)
            st.touched = self._clock()
            return st

    async def _ensure_async(self, session_id: str) -> _Session:
        """`_ensure` from the loop: a cached session costs only the lock; a cold one loads on an executor."""
        with self._lock:
            st = self._sessions.get(session_id)
            if st is not None:
                st.touched = self._clock()
                return st
        return await asyncio.get_running_loop().run_in_executor(None, self._ensure, session_id)

    def _persist(self, session_id: str, suffix: str, obj: object, *, fsync: bool = False) -> None:
        if not self.root.is_dir():
            private_dir(self.root)
        write_json_private(self._file(session_id, suffix), obj, fsync=fsync)

    def _flush(self, session_id: str, *suffixes: str) -> None:
        """Write the session's CURRENT control/request state: snapshotted under the lock, written outside it,
        serialized by `_io_lock` so a later write always carries the later state."""
        with self._io_lock:
            with self._lock:
                st = self._sessions.get(session_id)
                if st is None:
                    return
                texts = {}
                for suffix in suffixes:
                    obj = {"control.json": st.control, "requests.json": st.requests,
                           "answered.json": st.answered}[suffix]
                    texts[suffix] = json.dumps(obj, separators=(",", ":"))
            if not self.root.is_dir():
                private_dir(self.root)
            for suffix, text in texts.items():
                write_text_private(self._file(session_id, suffix), text)

    async def _flush_async(self, session_id: str, *suffixes: str) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._flush, session_id, *suffixes)

    # ── the identity fence ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _caller_session(identity, verb: str) -> str:
        if identity is None:
            raise _refuse(agent_vocabulary.ERR_NOT_A_RUNNER, f"{verb}: the caller is not a live runner")
        session_id = str(getattr(identity, "session_id", "") or "")
        if not valid_session_id(session_id):
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: the caller's runner carries no session")
        return session_id

    def _fence(self, identity, args: object, verb: str) -> str:
        """The caller's own session id, which `args["session_id"]` must name exactly."""
        session_id = self._caller_session(identity, verb)
        if not isinstance(args, dict):
            raise _bad(f"{verb}: args must be an object")
        named = args.get("session_id")
        if not isinstance(named, str) or not named:
            raise _bad(f"{verb} needs session_id")
        if named != session_id:
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: a caller may name only its own session")
        return session_id

    def _registered(self, identity, session_id: str, verb: str) -> None:
        """Refuse unless a launcher recorded `session_id` as an ACP run (`register`) of the caller's own task:
        a legacy run (a VM guest speaks as its VM) must neither make itself an ACP one nor park on its lanes."""
        record = self.store.get(session_id)
        if record is None:
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: that session was not launched as an ACP run")
        if identity.task_id is None or str(record.get("task_id")) != str(identity.task_id):
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: that session belongs to another task")

    def register(self, *, session_id: str, task_id: object, provider: str, key_sign_in: bool = False) -> dict:
        """The launcher's record of an ACP run (`acp_runtime.register_session`), made before its host starts:
        the only way a session becomes one this hub serves. Idempotent (`SessionStore.open`). `key_sign_in`: its
        launch names `authenticate`, so `account_key` may serve it once."""
        if not valid_session_id(session_id):
            raise ValueError("register: not a session id")
        if key_sign_in:
            self.store.set_key_sign_in(session_id, task_id=str(task_id), provider=provider)
        return self.store.open(session_id=session_id, task_id=str(task_id), provider=provider)

    # ── the sign-in key ─────────────────────────────────────────────────────────────────────────────────

    def account_key(self, identity, args: dict, *, fetch: Callable[[str], str]) -> dict:
        """`runtime.account_key`: the key of the API-key account the caller's runner record leased (`fetch`, the
        broker's `account_api_key`), for the host to send in ACP `authenticate`. The account is the identity's,
        never an argument; served once per session, and only to a session whose launch names `authenticate`
        (`register(key_sign_in=True)`). The one read is spent before the fetch, so a failed fetch fails the attempt
        closed rather than leaving a read for another caller. The answer is a LIVE KEY: never logged — refusals name
        the account."""
        verb = "runtime.account_key"
        session_id = self._fence(identity, args, verb)
        record = self.store.get(session_id)
        if record is None or str(record.get("task_id")) != str(identity.task_id):
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: that session was not launched as this task's ACP run")
        slug = str(getattr(identity, "api_key_account", "") or "")
        taken = self.store.take_key_sign_in(session_id)
        if not taken:
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: session {session_id}'s launch signs in with no key")
        if taken != "pending":
            log.warning("acp-runtime: session %s asked for its account key a second time — refused", session_id)
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: session {session_id}'s key was already served")
        if not slug:
            raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG, f"{verb}: session {session_id} leased no API-key account")
        key = fetch(slug)
        if not key:
            raise _bad(f"{verb}: account {slug} has no key")
        log.info("acp-runtime: session %s was served the key of account %s", session_id, slug)
        return {"key": key}

    # ── events ──────────────────────────────────────────────────────────────────────────────────────────

    def events_append(self, identity, args: dict) -> dict:
        session_id = self._fence(identity, args, "runtime.events_append")
        from acp import events as acp_events  # noqa: PLC0415 — the normaliser's vocabulary, only on this verb

        events = args.get("events")
        if not isinstance(events, list) or not events:
            raise _bad("runtime.events_append needs a non-empty events list")
        if len(events) > MAX_EVENTS_PER_BATCH:
            raise _bad(f"runtime.events_append takes at most {MAX_EVENTS_PER_BATCH} events")
        batch_id = args.get("batch_id")
        if batch_id is not None and not _short_id(batch_id):
            raise _bad("runtime.events_append: batch_id must be a short string")
        task_id = str(identity.task_id)
        stamped: list[dict] = []
        native_ref = ""
        for event in events:
            if not isinstance(event, dict) or event.get("kind") not in acp_events.KINDS:
                raise _bad("runtime.events_append: every event is an object with a known kind")
            out = dict(event)
            out["session_id"] = session_id
            out["task_id"] = task_id
            out.pop("seq", None)
            if isinstance(event.get("native_session_ref"), str) and event["native_session_ref"]:
                native_ref = event["native_session_ref"]
            stamped.append(out)
        self._registered(identity, session_id, "runtime.events_append")
        st = self._ensure(session_id)
        with st.append_lock:
            if batch_id is not None:
                for seen, first, last in st.batches:
                    if seen == batch_id:
                        return {"first_seq": first, "last_seq": last}
            first = st.seq + 1
            lines = []
            for offset, event in enumerate(stamped):
                event["seq"] = first + offset
                line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
                if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
                    raise _bad(f"runtime.events_append: an event over {MAX_EVENT_BYTES} bytes")
                lines.append(line)
            data = ("\n".join(lines) + "\n").encode("utf-8")
            if st.events_bytes + len(data) > MAX_SESSION_EVENT_BYTES:
                if not st.budget_logged:
                    st.budget_logged = True
                    log.warning("acp-runtime: session %s events file is at its %d-byte budget — refusing "
                                "further appends", session_id, MAX_SESSION_EVENT_BYTES)
                raise _refuse("busy", "runtime.events_append: the session's event log is full")
            last = first + len(stamped) - 1
            # The counter first (synced): a crash between the two leaves a gap, never a reused seq.
            self._persist(session_id, "seq", last, fsync=True)
            st.seq = last
            fd = _open_append(self._file(session_id, "events.jsonl"))
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            st.events_bytes += len(data)
            if batch_id is not None:
                st.batches.append([batch_id, first, last])
                st.batches = st.batches[-MAX_BATCH_IDS:]
                self._persist(session_id, "batches.json", st.batches)
            if native_ref:
                self.store.set_native_ref(session_id, native_ref)
            if native_ref:
                self._meter_jaah_account(session_id, native_ref, identity)
        starts = [e for e in stamped if e.get("kind") == "session_start"]
        if starts:
            self._drop_stale_intents(session_id, started=_epoch_of(starts[-1].get("ts")))
        log.info("acp-runtime: session %s appended seq %d-%d", session_id, first, last)
        self._kick_forwarder(session_id)
        return {"first_seq": first, "last_seq": last}

    def _meter_jaah_account(self, session_id: str, native_ref: str, identity) -> None:
        """A Jaah-account session's transcript is the one the agent names (`native_ref`), not the node's id:
        start the Bedrock usage tail on it the first time the host reports it. The tail reads the file from its
        start, so a prompt that ran before this registration is still metered. Under the session's append lock,
        so two batches never register at once.

        The ref names a FILE and comes from the host (the runner's uid), so only a canonical UUID is taken, and the
        first one tailed is pinned: a later ref can neither move the meter onto another session's transcript nor
        drop the calls not yet posted. A registration that failed is retried on the next batch. Never raises: the
        batch is already appended, and metering is best effort (AWS's invocation log still bills)."""
        try:
            record = self.store.get(session_id) or {}
            meter = record.get("jaah_account")
            if not isinstance(meter, dict):
                return
            try:
                ref = str(uuid.UUID(native_ref))
            except ValueError:
                ref = ""
            if ref != native_ref:
                log.warning("acp-runtime: session %s reported a native session ref that is no canonical UUID — "
                            "its Jaah-account calls are billed only by AWS's invocation log", session_id)
                return
            tailed = meter.get("tailed_ref")
            if tailed:
                if tailed != ref:
                    log.warning("acp-runtime: session %s changed its native session ref — the usage tail stays "
                                "on the first", session_id)
                return
            import bedrock_usage_tail  # noqa: PLC0415 — the daemon's metering, only for a Jaah-account session

            if bedrock_usage_tail.register(
                    jaah_session_id=str(meter.get("jaah_session_id") or ""),
                    pane_pid=getattr(identity, "pane_pid", None), config_dir=str(meter.get("config_dir") or ""),
                    worktree=str(meter.get("worktree") or ""), native_session_id=ref):
                self.store.set_jaah_account(session_id, task_id=str(record.get("task_id") or ""),
                                           provider=str(record.get("provider") or ""),
                                           meter={**meter, "tailed_ref": ref})
        except Exception:  # noqa: BLE001 — see the docstring
            log.warning("acp-runtime: session %s: could not start its Jaah-account usage tail", session_id,
                        exc_info=True)

    def _drop_stale_intents(self, session_id: str, *, started: float | None = None) -> None:
        """A new attempt's `session_start`: an intent queued for the previous attempt's host is not this one's.
        The cut is when the host STARTED (the event's own `ts`), never later than now, so a steer or close sent
        while the new host was still starting is kept for it."""
        cut = self._clock() if started is None else min(self._clock(), started)
        with self._lock:
            st = self._sessions.get(session_id)
            if st is None:
                return
            # An `answer` survives: it is an operator's words queued as the next prompt, owed to whichever
            # attempt runs the session.
            kept = [i for i in st.control["intents"]
                    if float(i.get("enqueued_at", 0)) >= cut or i.get("type") == "answer"]
            dropped = len(st.control["intents"]) - len(kept)
            st.control["intents"] = kept
        if dropped:
            self._flush(session_id, "control.json")
            log.info("acp-runtime: session %s restarted — dropped %d stale intent(s)", session_id, dropped)

    def _behind(self, session_id: str) -> bool:
        """Whether the session has events past its forward cursor."""
        seq = read_json(self._file(session_id, "seq"))
        cursor = read_json(self._file(session_id, "fwd"))
        done = cursor.get("seq", 0) if isinstance(cursor, dict) else 0
        return isinstance(seq, int) and isinstance(done, int) and seq > done

    def _kick_forwarder(self, session_id: str) -> None:
        if self._forward is None:
            return
        with self._fwd_lock:
            if self._fwd_thread is None or not self._fwd_thread.is_alive():
                self._fwd_thread = threading.Thread(target=self._forwarder_loop, name="acp-events-forwarder",
                                                    daemon=True)
                self._fwd_thread.start()
            if session_id not in self._fwd_pending:
                self._fwd_pending.add(session_id)
                self._fwd_queue.put(session_id)

    def _forwarder_loop(self) -> None:
        while True:
            with self._fwd_lock:
                due = min((d for d, _ in self._fwd_retry.values()), default=None)
            timeout = None if due is None else max(0.0, due - time.monotonic())
            try:
                session_id = self._fwd_queue.get(timeout=timeout)
            except queue.Empty:
                now = time.monotonic()
                with self._fwd_lock:
                    ready = [sid for sid, (d, _) in self._fwd_retry.items() if d <= now]
                for sid in ready:
                    self._forward_once(sid)
                continue
            with self._fwd_lock:
                self._fwd_pending.discard(session_id)
            try:
                self._forward_once(session_id)
            finally:
                self._fwd_queue.task_done()

    def _forward_once(self, session_id: str) -> None:
        try:
            self.forward_pending(session_id)
        except Exception as exc:  # noqa: BLE001 — retried with backoff, and on the session's next append
            with self._fwd_lock:
                backoff = self._fwd_retry.get(session_id, (0.0, FORWARD_BACKOFF_S / 2))[1] * 2
                backoff = min(backoff, FORWARD_BACKOFF_MAX_S)
                self._fwd_retry[session_id] = (time.monotonic() + backoff, backoff)
            log.warning("acp-runtime: forwarding session %s failed (%s: %s) — retrying in %.0fs",
                        session_id, type(exc).__name__, exc, backoff)
        else:
            with self._fwd_lock:
                self._fwd_retry.pop(session_id, None)

    def forward_pending(self, session_id: str) -> int:
        """Post every event past the session's forward cursor, `MAX_FORWARD_EVENTS`/`MAX_FORWARD_BYTES` at a
        time; advance the cursor on success, and past a batch the CP refuses outright (4xx: re-posting earns
        the same refusal, so it is dead-lettered, logged by seq only). Raises any other failure. Returns how
        many were accepted. Never under the hub lock: the cursor is the forwarder's own (`_fwd_io_lock`)."""
        posted = 0
        with self._fwd_io_lock:
            while True:
                record = self.store.get(session_id)
                cursor = read_json(self._file(session_id, "fwd"))
                done_seq, offset = (cursor.get("seq", 0), cursor.get("offset", 0)) if isinstance(
                    cursor, dict) else (0, 0)
                batch, end = self._read_events(session_id, int(offset), int(done_seq))
                if not batch or record is None:
                    return posted
                try:
                    self._forward(session_id, str(record.get("task_id", "")), batch)
                    posted += len(batch)
                except urllib.error.HTTPError as exc:
                    if exc.code not in _DEAD_LETTER_STATUSES:
                        raise
                    detail = _refusal_detail(exc)
                    log.warning("acp-runtime: the CP refused session %s seq %d-%d — dead-lettered: %s",
                                session_id, batch[0]["seq"], batch[-1]["seq"], detail)
                    if exc.code in _PLACEHOLDER_STATUSES:
                        posted += self._post_placeholders(session_id, record, batch, detail)
                self._persist(session_id, "fwd", {"seq": batch[-1]["seq"], "offset": end})

    def _post_placeholders(self, session_id: str, record: dict, batch: list[dict], detail: str) -> int:
        """A batch the CP refused for what it IS: post one content-free `error` event per refused seq in its place,
        so the refusal shows on the session's own stream and the stream stays gapless (a gap 409s every later
        batch). Only the seq is the refused event's: every other field is the hub's own, so whatever the CP refused
        in the event is not sent again. A refused placeholder batch is dead-lettered too (logged); any other failure
        raises, so the cursor stays and the whole batch is tried again."""
        from acp import events as acp_events  # noqa: PLC0415 — the schema's `ts` spelling, only on a refusal

        task_id = str(record.get("task_id", ""))
        envelope = {"v": 1, "ts": acp_events.iso_ms(), "session_id": session_id, "task_id": task_id,
                    "provider": str(record.get("provider") or "") or "unknown", "kind": "error", "status": "failed",
                    "data": {"class": "agent", "message": f"events not recorded: the control plane refused them "
                                                          f"({detail})"}}
        placeholders = [{**envelope, "seq": event["seq"]} for event in batch]
        try:
            self._forward(session_id, task_id, placeholders)
        except urllib.error.HTTPError as exc:
            if exc.code not in _DEAD_LETTER_STATUSES:
                raise
            log.warning("acp-runtime: the CP refused session %s's placeholders for seq %d-%d too: %s",
                        session_id, batch[0]["seq"], batch[-1]["seq"], _refusal_detail(exc))
            return 0
        return len(placeholders)

    def _read_events(self, session_id: str, offset: int, after_seq: int) -> tuple[list[dict], int]:
        out: list[dict] = []
        total = 0
        try:
            fd = os.open(self._file(session_id, "events.jsonl"), os.O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_BINARY)
        except OSError:
            return out, offset
        with os.fdopen(fd, "rb") as fh:
            fh.seek(offset)
            while len(out) < MAX_FORWARD_EVENTS:
                line = fh.readline()
                if not line.endswith(b"\n"):
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    offset = fh.tell()
                    continue
                if isinstance(event, dict) and isinstance(event.get("seq"), int) and event["seq"] > after_seq:
                    if out and total + len(line) > MAX_FORWARD_BYTES:
                        break
                    out.append(event)
                    total += len(line)
                offset = fh.tell()
        return out, offset

    # ── the control lane ────────────────────────────────────────────────────────────────────────────────

    def enqueue_control(self, session_id: str, intent: dict) -> bool:
        """Queue an operator intent `{id, type, payload}` for the session's host. False when the session is
        not an ACP one or the intent is malformed. A re-sent id is accepted once (True, nothing queued)."""
        if not self.is_acp(session_id) or not isinstance(intent, dict):
            return False
        intent_id, kind, payload = intent.get("id"), intent.get("type"), intent.get("payload", {})
        if not _short_id(intent_id) or kind not in INTENT_TYPES or not isinstance(payload, dict):
            return False
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_INTENT_BYTES:
            return False
        st = self._ensure(session_id)
        with self._lock:
            self._expire(st)
            if any(i.get("id") == intent_id for i in st.control["intents"]):
                return True
            if len(st.control["intents"]) >= MAX_QUEUED_INTENTS:
                full = True
            else:
                full = False
                st.control["next"] += 1
                st.control["intents"].append({"cursor": st.control["next"], "id": intent_id, "type": kind,
                                              "payload": payload, "enqueued_at": self._clock()})
                waiters, st.control_waiters = st.control_waiters, []
                _wake(waiters)
        if full:
            log.warning("acp-runtime: session %s control queue full — refusing intent %s", session_id, intent_id)
            return False
        self._flush(session_id, "control.json")
        log.info("acp-runtime: session %s queued %s intent %s", session_id, kind, intent_id)
        return True

    def _expire(self, st: _Session) -> bool:
        horizon = self._clock() - INTENT_RETENTION_S
        kept = [i for i in st.control["intents"] if float(i.get("enqueued_at", 0)) >= horizon]
        changed = len(kept) != len(st.control["intents"])
        st.control["intents"] = kept
        return changed

    async def control_wait(self, identity, args: dict, *, timeout: float = MAX_WAIT_S) -> dict:
        session_id = self._fence(identity, args, "runtime.control_wait")
        after = args.get("after", 0)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise _bad("runtime.control_wait: after must be a non-negative integer")
        await asyncio.to_thread(self._registered, identity, session_id, "runtime.control_wait")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(float(timeout), MAX_WAIT_S))
        st = await self._ensure_async(session_id)
        while True:
            fut = loop.create_future()
            result = None
            with self._lock:
                st = self._sessions.setdefault(session_id, st)
                st.touched = self._clock()
                # A cursor past the highest issued one would ack intents not yet queued.
                after = min(after, int(st.control["next"]))
                kept = [i for i in st.control["intents"] if int(i.get("cursor", 0)) > after]
                changed = len(kept) != len(st.control["intents"])
                st.control["intents"] = kept
                changed = self._expire(st) or changed
                ready = [{k: i[k] for k in ("cursor", "id", "type", "payload")} for i in st.control["intents"]]
                remaining = deadline - loop.time()
                if ready or remaining <= 0:
                    result = {"intents": ready, "cursor": max((i["cursor"] for i in ready), default=after)}
                else:
                    st.control_waiters.append((loop, fut))
            if changed:
                await self._flush_async(session_id, "control.json")
            if result is not None:
                return result
            try:
                await asyncio.wait_for(fut, remaining)
            except asyncio.TimeoutError:
                pass
            finally:
                with self._lock:
                    st.control_waiters = [w for w in st.control_waiters if w[1] is not fut]

    # ── user-input requests ─────────────────────────────────────────────────────────────────────────────

    def _prune_requests(self, st: _Session) -> bool:
        """Forget delivered decisions past their grace and requests open past `REQUEST_RETENTION_S`; a
        waiter on a forgotten one is woken (to read it as unknown). Caller holds the lock."""
        now = self._clock()
        gone = [rid for rid, e in st.requests.items()
                if (e.get("delivered_at") is not None and now - float(e["delivered_at"]) > DECIDED_GRACE_S)
                or now - float(e.get("opened_at", 0)) > REQUEST_RETENTION_S]
        for rid in gone:
            del st.requests[rid]
            _wake(st.request_waiters.pop(rid, []))
        return bool(gone)

    def request_open(self, identity, args: dict) -> dict:
        session_id = self._fence(identity, args, "runtime.request_open")
        from acp import requests as acp_requests  # noqa: PLC0415 — only on this verb

        try:
            request = acp_requests.validate_request(args.get("request"))
        except ValueError as exc:
            raise _bad(f"runtime.request_open: {exc}") from None
        if not valid_session_id(request.id):
            raise _bad("runtime.request_open: the request id must be a short plain id")
        self._registered(identity, session_id, "runtime.request_open")
        st = self._ensure(session_id)
        with self._lock:
            self._prune_requests(st)
            if request.id in st.requests:
                return {"request_id": request.id}
            if (sum(1 for e in st.requests.values() if e.get("delivered_at") is None) >= MAX_OPEN_REQUESTS
                    or len(st.requests) >= MAX_HELD_REQUESTS):
                raise _refuse("busy", "runtime.request_open: too many open requests")
            st.requests[request.id] = {"request": request.to_wire(), "opened_at": self._clock(),
                                       "decision": None, "answers": None}
        self._flush(session_id, "requests.json")
        log.info("acp-runtime: session %s opened request %s", session_id, request.id)
        return {"request_id": request.id}

    def request_cancel(self, identity, args: dict) -> dict:
        """The host withdrew its own request (the agent cancelled it): decided `cancel`, any waiter woken,
        forgotten after the grace period. Only the caller's own session is searched."""
        session_id = self._caller_session(identity, "runtime.request_cancel")
        request_id = args.get("request_id") if isinstance(args, dict) else None
        if not _short_id(request_id):
            raise _bad("runtime.request_cancel needs request_id")
        self._registered(identity, session_id, "runtime.request_cancel")
        st = self._ensure(session_id)
        with self._lock:
            self._prune_requests(st)
            entry = st.requests.get(request_id)
            if entry is None:
                return {"cancelled": False}
            if not entry.get("decision"):
                entry["decision"], entry["answers"] = "cancel", None
            if entry.get("delivered_at") is None:
                entry["delivered_at"] = self._clock()
            _wake(st.request_waiters.pop(request_id, []))
        self._flush(session_id, "requests.json")
        log.info("acp-runtime: session %s cancelled request %s", session_id, request_id)
        return {"cancelled": True}

    async def request_wait(self, identity, args: dict, *, timeout: float = MAX_WAIT_S) -> dict:
        session_id = self._caller_session(identity, "runtime.request_wait")
        request_id = args.get("request_id") if isinstance(args, dict) else None
        if not _short_id(request_id):
            raise _bad("runtime.request_wait needs request_id")
        await asyncio.to_thread(self._registered, identity, session_id, "runtime.request_wait")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(float(timeout), MAX_WAIT_S))
        st = await self._ensure_async(session_id)
        while True:
            fut = loop.create_future()
            out = None
            with self._lock:
                st = self._sessions.setdefault(session_id, st)
                st.touched = self._clock()
                changed = self._prune_requests(st)
                entry = st.requests.get(request_id)
                if entry is not None and entry.get("decision"):
                    # Kept for the grace period: a retry after a lost reply reads the same decision.
                    if entry.get("delivered_at") is None:
                        entry["delivered_at"] = self._clock()
                        changed = True
                    out = {"decision": entry["decision"]}
                    if entry.get("answers") is not None:
                        out["answers"] = entry["answers"]
                elif entry is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        out = {"pending": True}
                    else:
                        st.request_waiters.setdefault(request_id, []).append((loop, fut))
            if changed:
                await self._flush_async(session_id, "requests.json")
            if entry is None:
                # Only the caller's own session is ever searched: another session's id reads as unknown.
                raise _refuse(agent_vocabulary.ERR_FORBIDDEN_ARG,
                              "runtime.request_wait: no such open request in the caller's session")
            if out is not None:
                return out
            try:
                await asyncio.wait_for(fut, remaining)
            except asyncio.TimeoutError:
                pass
            finally:
                with self._lock:
                    left = [w for w in st.request_waiters.get(request_id, []) if w[1] is not fut]
                    if left:
                        st.request_waiters[request_id] = left
                    else:
                        st.request_waiters.pop(request_id, None)

    def resolve_request(self, session_id: str, *, request_id: str | None, decision: str,
                        answers: dict | None, question: str | None = None, intent_id: str | None = None) -> bool:
        """Decide one open request (`request_id` when it is open, else the session's oldest undecided one).

        With `question` (an operator's answer, `answers={"answer": text}`): `acp.requests.apply_answer` fills the
        request's questions — one round's answer, decided once every question is answered, or `question=""`'s
        batch hand-off, decided at once — as `{question_id: text, ...}`. Without it: decided at once with
        `answers` as given. `intent_id` dedups: a repeat is True and changes nothing. False when
        there is nothing to decide or the decision is not one of `requests.DECISIONS`."""
        from acp import requests as acp_requests  # noqa: PLC0415

        try:
            decision = acp_requests.validate_decision(decision)
        except ValueError:
            return False
        if answers is not None and not isinstance(answers, dict):
            return False
        text = answers.get("answer") if question is not None and answers else None
        if question is not None and not isinstance(text, str):
            return False
        if not self.is_acp(session_id):
            return False
        st = self._ensure(session_id)
        with self._lock:
            self._prune_requests(st)
            if intent_id is not None and intent_id in st.answered:
                return True
            undecided = sorted(((e.get("opened_at", 0), rid) for rid, e in st.requests.items()
                                if not e.get("decision")))
            entry = st.requests.get(request_id) if request_id is not None else None
            if entry is None or entry.get("decision"):
                if request_id is not None and question is None:
                    return False
                if not undecided:
                    return False
                fallback = st.requests[undecided[0][1]]
                if request_id is not None and not any(
                        isinstance(q, dict)
                        and acp_requests.question_key(q.get("question")) == acp_requests.question_key(question)
                        for q in fallback.get("request", {}).get("questions", [])):
                    # The named request is gone (cancelled, pruned): its late answer must not fill a newer,
                    # unrelated one — False sends it down the queued-answer path instead.
                    return False
                request_id = undecided[0][1]
                entry = fallback
            rest: list[tuple[str, str]] = []
            if question is None:
                entry["decision"], entry["answers"] = decision, answers
            else:
                questions = [q for q in entry.get("request", {}).get("questions", []) if isinstance(q, dict)]
                got = dict(entry["answers"]) if isinstance(entry.get("answers"), dict) else {}
                decided_now, rest = acp_requests.apply_answer(questions, got, question, text)
                if decided_now is None:
                    return False
                entry["answers"] = got
                if decided_now:
                    entry["decision"] = decision
            if intent_id is not None:
                st.answered = (st.answered + [intent_id])[-MAX_ANSWER_INTENTS:]
            decided = bool(entry.get("decision"))
            if decided:
                _wake(st.request_waiters.pop(request_id, []))
        self._flush(session_id, "requests.json", "answered.json")
        if rest:
            # Batch rounds this request never asked (another channel's question): the agent still reads them, framed
            # as every queued answer is, as its next prompt.
            import answer_delivery  # noqa: PLC0415 — deferred, as reconcile's ACP delivery imports it

            text = "\n\n".join(answer_delivery.compose_answer_message(q, a) for q, a in rest)
            if not self.enqueue_control(session_id, {"id": f"answer-rest-{intent_id or request_id}",
                                                     "type": "answer", "payload": {"text": text}}):
                log.warning("acp-runtime: session %s request %s — %d answered round(s) it never asked could not "
                            "be queued for the agent", session_id, request_id, len(rest))
        log.info("acp-runtime: session %s request %s %s", session_id, request_id,
                 f"decided {decision}" if decided else "answered in part")
        return True

    def open_requests(self, session_id: str) -> list[str]:
        """The session's undecided request ids, oldest first."""
        if not valid_session_id(session_id):
            return []
        st = self._ensure(session_id)
        with self._lock:
            return [rid for _, rid in sorted((e.get("opened_at", 0), rid) for rid, e in st.requests.items()
                                             if not e.get("decision"))]

    def is_acp(self, session_id: str) -> bool:
        return valid_session_id(session_id) and self.store.is_acp(session_id)

    # ── the sweep ───────────────────────────────────────────────────────────────────────────────────────

    def maybe_sweep(self) -> None:
        """Start `sweep` on its own thread when the last one is `SWEEP_INTERVAL_S` old (first call: now, so a
        daemon start re-forwards what a crash left behind). Never blocks the caller (the broker's loop)."""
        now = time.monotonic()
        with self._sweep_lock:
            if now - self._last_sweep < SWEEP_INTERVAL_S:
                return
            self._last_sweep = now
        threading.Thread(target=self._sweep_logged, name="acp-runtime-sweep", daemon=True).start()

    def _sweep_logged(self) -> None:
        try:
            self.sweep()
        except Exception as exc:  # noqa: BLE001 — the next sweep tries again
            log.warning("acp-runtime: sweep failed (%s: %s)", type(exc).__name__, exc)

    def sweep(self) -> None:
        """Evict in-memory state idle past `SESSION_IDLE_EVICT_S`; delete the files of a session untouched
        past `SESSION_FILE_RETENTION_S`; kick the forwarder for a session whose cursor is behind its seq."""
        now = self._clock()
        with self._lock:
            for sid, st in list(self._sessions.items()):
                if (now - st.touched > SESSION_IDLE_EVICT_S and not st.control_waiters
                        and not st.request_waiters and not st.append_lock.locked()):
                    del self._sessions[sid]
        files: dict[str, list[Path]] = {}
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                files.setdefault("", []).append(Path(entry.path))  # a temp file a crash left
                continue
            suffix = next((x for x in _SUFFIXES if name.endswith("." + x)), None)
            if suffix is not None and valid_session_id(name[:-len(suffix) - 1]):
                files.setdefault(name[:-len(suffix) - 1], []).append(Path(entry.path))
        for sid, paths in files.items():
            try:
                newest = max(os.lstat(p).st_mtime for p in paths)
            except OSError:
                continue
            with self._lock:
                live = bool(sid) and sid in self._sessions
            if now - newest > SESSION_FILE_RETENTION_S and not live:
                if sid and self._forward is not None and self._behind(sid):
                    continue  # never delete events the CP has not received
                for p in paths:
                    # The `seq` high-water mark stays: a later attempt on this session id must not restart at 1,
                    # which the CP's seq-idempotent ingest would silently drop as duplicates.
                    if p.name.endswith(".seq"):
                        continue
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                if sid:
                    log.info("acp-runtime: session %s untouched for a week — files deleted", sid)
                continue
            if sid and self._forward is not None:
                seq = read_json(self._file(sid, "seq"))
                cursor = read_json(self._file(sid, "fwd"))
                done = cursor.get("seq", 0) if isinstance(cursor, dict) else 0
                if isinstance(seq, int) and isinstance(done, int) and seq > done:
                    self._kick_forwarder(sid)


# A forward the CP refused for what the batch IS (malformed, unknown session, too large): re-posting earns the
# same answer, so it is dead-lettered. Every other status (auth rotation, throttling, 5xx) is retried.
_DEAD_LETTER_STATUSES = frozenset({400, 404, 409, 413, 422})
# The refusals of a batch's CONTENT, answered with placeholders (`_post_placeholders`); a 404 (no such session on
# the CP) or a 409 (a seq conflict) would refuse the placeholders the same way.
_PLACEHOLDER_STATUSES = frozenset({400, 413, 422})


def _refusal_detail(exc: BaseException) -> str:
    """A CP refusal as `rejected: HTTP <status> — <reason>`: the broker's one parse of the CP's body
    (`cred_broker._rejected_reason`: a string `detail`, or `msg`/`loc` per validation entry — never the echoed
    `input`), redacted as any other `error` message, since it is stored on the session."""
    import cred_broker  # noqa: PLC0415 — the single home of the refusal parse, only on a refusal
    from acp import events as acp_events  # noqa: PLC0415 — the one redactor every error message passes

    return acp_events.redact(cred_broker._rejected_reason(exc))


def _epoch_of(ts: object) -> float | None:
    """An event's ISO-8601 `ts` as an epoch, or None when it does not parse."""
    from datetime import datetime  # noqa: PLC0415

    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


_HUB: RuntimeHub | None = None
_HUB_LOCK = threading.Lock()


def _post_events(session_id: str, task_id: str, events: list) -> None:
    import control_plane_api  # noqa: PLC0415 — the CP client only once there is something to post

    control_plane_api.post_session_events(session_id, task_id, events)


def hub() -> RuntimeHub:
    """This daemon's one hub, rooted in the daemon's own state dir. The root is made 0700 (`private_dir`):
    the daemon runs with `UMask=0002` in the runners' group, so without it any runner could plant a file or
    a symlink there. Each call may start the hourly sweep (on its own thread)."""
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            import env_helpers  # noqa: PLC0415 — stdlib-only leaf

            _HUB = RuntimeHub(env_helpers.daemon_state_dir(RUNS_DIR), forward=_post_events)
        current = _HUB
    current.maybe_sweep()
    return current
