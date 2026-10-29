#!/usr/bin/env python3
"""runner-status.py — the runner-lifecycle settle client fired by a fleet runner's session hooks
(wired by `fleet_launch.build_settings`).

  start           SessionStart: best-effort `ready -> active` reinforcement of the dispatcher's claim.
  heartbeat       Stop (every turn): "still working"; a break-for-replace if the turn ended on an account wall.
  wallcheck       PreToolUse: a break-for-replace the moment Claude Code's usage-limit grace note lands.
  stopfailure     StopFailure: classify the break and request a replace.
  console_exit    SessionEnd: an operator's prompt `/exit` completes an interactive chat. Optional
                  `--reason R [--pane-marker M]`: this CLI's quit signature (`agents.QuitSignature`).
  console_resume  UserPromptSubmit: a parked chat goes back to `active`.
  settle          The idle-confirmed terminal ladder: not active -> no-op; active WITH evidence -> re-post
                  the terminal (`settle_task` commits then may refuse, so nobody else will); active with NO
                  evidence -> a broken session: record `session.error` (cause from `break_cause`), then
                  `task.fail`, the cause riding in its reason.

Every subcommand is best-effort, idempotent, always exits 0, no-ops without $CONTROL_TASK_ID, and logs
per task. This hook holds NO credential: every call is a verb on the daemon's socket (`agent_socket`;
why: `agent_vocabulary.py`). No task id is ever sent (the daemon stamps it from peer identity and refuses
one that is named), and a refusal is never worked around.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent

# Path-loaded: a hook runs as a script, so its sibling is not importable by name. Stdlib-only and raises
# nothing at import (an import failure kills the session).
_bc_spec = importlib.util.spec_from_file_location("break_cause", _SELF_DIR / "break_cause.py")
break_cause = importlib.util.module_from_spec(_bc_spec)
_bc_spec.loader.exec_module(break_cause)

# `agent_socket` lives one directory up; APPENDED to sys.path (never inserted) so nothing in the hooks
# directory can shadow a stdlib module. Stdlib-only and raises nothing at import.
_JAAHD_DIR = str(_SELF_DIR.parent)
if _JAAHD_DIR not in sys.path:
    sys.path.append(_JAAHD_DIR)
# `environment` comes in on the same append, for the same reasons.
import agent_socket  # noqa: E402
import attempt_end  # noqa: E402
import environment  # noqa: E402
import runner_env  # noqa: E402

# The request timeout is `agent_socket.TIMEOUT_S`.

# Wait for the FIRST byte of stdin. Claude Code writes and closes at spawn, so this is normally instant;
# it stops an inherited never-written pipe from parking the turn. Losing the payload only degrades.
_STDIN_WAIT_SECONDS = 0.2

# Size cap on that read (a StopFailure event is a few hundred bytes), and its chunk.
_STDIN_MAX_CHARS = 1 << 20
_STDIN_CHUNK_CHARS = 4096

# `console_exit`'s DEFAULT reason, used when its chain passes no `--reason` (a chain rendered before the flag
# existed): Claude Code's human quitting at the prompt. The enum is `clear`,
# `resume`, `logout`, `prompt_input_exit`, `other` — there is NO `"exit"` (verified on Claude Code 2.1.251
# by typing `/exit`); a guessed value would make the door a silent no-op. `clear` especially must not
# complete: it starts a fresh session in the same worktree. A literal, not imported from
# `user-session-end.py`: a hook that fails to import kills its session.
_CONSOLE_EXIT_REASON = "prompt_input_exit"

# Wall clock on reading this runner's own pane for a quit signature's marker. One measured CLI kills a
# SessionEnd hook 0.5-1 s after starting it, and the
# socket call must still go out inside that.
_PANE_CAPTURE_TIMEOUT_S = 0.4

# Where a quit marker may be: the bottom of the pane, where the CLI draws its quit text. Measured, it is in
# the last 2-6 non-blank lines (a status or debug line or two can follow it); anything higher is scrollback
# the model may have written.
_QUIT_MARKER_TAIL_LINES = 12

# Box-drawing borders a CLI frames its exit summary in, stripped before matching so a boxed marker matches.
_BOX_CHARS = "\u2502\u256d\u256e\u256f\u2570\u2500"

# ── `error_type` → the break-cause vocabulary (StopFailure's race-free floor) ─────────────────────────
# StopFailure's payload carries the vendor's `error_type`, available instantly — unlike the transcript tail
# (`break_cause._TAIL_READ_ATTEMPTS`: the flush race). Mapped onto the existing `break_cause.CAUSES` (a
# cross-repo SSOT mirrored in `apps/api/app/runner_causes.py`), never new spellings; request- and
# server-shaped failures collapse into `api_error`. `unknown` and unrecognised values fall through.
_ERROR_TYPE_CAUSES = {
    # Account verdicts (`_ACCOUNT_BREAK_VERDICTS`, apps/api/app/routes/agent/runner_liveness.py): take the walled account
    # out of rotation even when the transcript read is blind.
    "rate_limit": "usage_limit",
    "billing_error": "credit_exhausted",
    # A dead/rejected credential: remedied by a re-login.
    "authentication_failed": "auth_expired",
    "oauth_org_not_allowed": "auth_expired",
    # Account on hold: somebody must act on the account (the money remedy), never a window that clears.
    "account_on_hold": "credit_exhausted",
    # Everything the API itself said no to.
    "overloaded": "api_error",
    "server_error": "api_error",
    "invalid_request": "api_error",
    "model_not_found": "api_error",
    "max_output_tokens": "api_error",
}

# The causes that make an ordinary `Stop` a break — the account-cannot-run family (`_ACCOUNT_BREAK_VERDICTS`),
# and the terminal refusals (`runner_causes.TERMINAL_CAUSES`: no later turn can succeed either).
# `timeout`/`api_error` are absent: the session's own retry may survive a transient error.
_TURN_END_WALL_CAUSES = ("usage_limit", "credit_exhausted", "auth_expired", "model_refused", "client_unsupported")


def _hook_payload() -> dict:
    """The hook event JSON Claude Code wrote to our stdin, or `{}` — never an exception, never a hang.

    Best-effort: the payload is a floor under the transcript read, so missing it costs nothing while
    failing loudly kills the session. A tty (manual run), silence past `_STDIN_WAIT_SECONDS`, bad JSON or
    a non-object all answer `{}`.

    Only `stopfailure`, `console_exit` and `heartbeat` may call this: hook chains share one stdin. The first two
    are the only command in their chain; `heartbeat` is the Stop chain's first reader, and the lane's Stop check
    after it only drains stdin. `test_settle_does_not_read_the_hook_payload`
    guards `settle`.
    Mirrors `user-session-end.py::_hook_payload` (hooks cannot import each other), plus the `select`
    gate: StopFailure fires while the session waits on us."""
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return {}
    try:
        if stream.isatty():
            return {}       # a manual invocation, not a hook event — nothing is coming
    except (OSError, ValueError):
        return {}
    try:
        if not select.select([stream], [], [], _STDIN_WAIT_SECONDS)[0]:
            return {}       # nothing piped in within the budget
    except (OSError, ValueError, TypeError):
        # Not selectable: still usually readable, and isatty already excluded the blocking case.
        pass
    # Bounded in time AND size: `select` only says ONE byte is ready, so a writer that sends `{` and holds
    # the pipe open would wedge the turn. `os.read` on the raw fd, not `stream.read(n)` — TextIOWrapper
    # loops until n chars or EOF, making the deadline decoration. A truncated payload just fails to parse.
    # A stream with no real fd cannot block, so it takes the plain read.
    deadline = time.monotonic() + _STDIN_WAIT_SECONDS
    try:
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        fd = -1
    if fd < 0:
        try:
            return _as_payload(stream.read(_STDIN_MAX_CHARS))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
    chunks: list[bytes] = []
    total = 0
    try:
        while total < _STDIN_MAX_CHARS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not select.select([fd], [], [], remaining)[0]:
                break
            chunk = os.read(fd, _STDIN_CHUNK_CHARS)
            if not chunk:
                break   # EOF — the normal end, since Claude Code closes as it spawns us
            chunks.append(chunk)
            total += len(chunk)
    except (OSError, ValueError):
        return {}
    try:
        raw = b"".join(chunks).decode("utf-8", "replace")
    except (UnicodeDecodeError, ValueError):
        return {}
    return _as_payload(raw)


def _as_payload(raw) -> dict:
    """The JSON object `raw` holds, or `{}` — the one place this file decides what counts as a payload.

    Shared by both read paths so they cannot disagree; a non-object has no `error_type` to read."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _payload_transcript(hook_payload: dict) -> str:
    """The `transcript_path` the hook payload names, but ONLY if it is one of ours — else `""`.

    Confined to `break_cause.worktree_root()` (realpath both sides, `+ os.sep` prefix): the payload path
    is vendor-supplied, and unconfined it would be a read oracle (`classify_transcript` copies a matched
    line into the stored `detail`) and would let the payload choose the fallback CP `session_id` fence.
    Anything else answers `""`."""
    candidate = hook_payload.get("transcript_path")
    if not isinstance(candidate, str) or not candidate.endswith(".jsonl"):
        return ""
    try:
        resolved = os.path.realpath(candidate)
        root = os.path.realpath(break_cause.worktree_root())
    except OSError:
        return ""
    if not root or not resolved.startswith(root + os.sep) or not os.path.isfile(resolved):
        return ""
    return resolved



def _log(*parts: str) -> None:
    """Per-task decision log (instrumentation only — never gates a decision)."""
    task_id = runner_env.task_id("none")
    log_dir = environment.hook_log_dir()
    try:
        os.makedirs(log_dir, exist_ok=True)
        stamp = __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(os.path.join(log_dir, f"runner-status-{task_id}.log"), "a") as f:
            f.write(f"{stamp} [task={task_id}] {' '.join(parts)}\n")
    except OSError:
        pass


def _env_ready() -> bool:
    """True iff this is a fleet runner we can report for: `$CONTROL_TASK_ID` set AND a daemon socket
    present. Either missing is a clean no-op. No token is checked: there is none (`agent_vocabulary`).
    `agent_socket.available()` is a shape check, so a permission failure surfaces at the call instead."""
    return bool(runner_env.task_id()) and agent_socket.available()


def _runner_session_id() -> str:
    """The identity to fence a settle/break report against: `CONTROL_RUNNER_SESSION_ID`, the same uuid
    `fleet_dispatch.launch_runner` passes as `--session-id` and the CP stamps onto
    `Task.runner_session_id`. Without it `state == active` names a TASK, not an ATTEMPT, and a stale
    runner's late report could end its replacement.

    Not the transcript filename (equal today, but only this env var is what the CP compares). Empty when
    absent; callers then fall back to the transcript-derived id."""
    return runner_env.runner_session_id()


def rs_start() -> int:
    """SessionStart claim: best-effort `ready -> active` for this task (the dispatcher already claimed at
    launch, so this is normally an idempotent no-op). The body is EMPTY: the daemon stamps `task_id` from
    peer identity and refuses a body that names one."""
    if not _env_ready():
        _log("start noop=env-missing")
        return 0
    try:
        agent_socket.call("session.start", {})
        _log("start posted claim-active")
    except agent_socket.AgentSocketRefused as exc:
        _log(f"start claim-active best-effort failed (daemon refused: {exc.reason}, non-fatal)")
    except agent_socket.AgentSocketUnavailable as exc:
        _log(f"start claim-active best-effort failed (daemon unreachable: {exc}, non-fatal)")
    return 0


def rs_settle() -> int:
    """Confirmed-idle terminal settle (ladder: module docstring; single home: `attempt_end.end_attempt`)."""
    if not _env_ready():
        _log("settle noop=env-missing")
        return 0
    attempt_end.end_attempt(record_break=_record_break, log=_log)
    return 0


def _classify_and_build_break_payload(
    *, observed_by: str, extra: dict | None = None, hook_payload: dict | None = None,
    verdict: tuple | None = None, transcript: str | None = None,
) -> tuple:
    """Classify this session's break off its own transcript and build the `session.error` payload.
    Returns `(payload, cause, detail)`; shared by every break observer. Always returns a usable cause
    (classification is local; `unknown` is the floor, never a guess).

    The transcript wins; `hook_payload`'s `error_type` is consulted ONLY when it answered `unknown`. The
    transcript carries a re-checkable evidence line and alone can tell `credit_exhausted` from
    `usage_limit` (different remedies; a spend cap may report as `rate_limit`). The payload is a floor
    for the flush race, enough to take a walled account out of rotation.

    A caller holding the verdict passes `verdict=(cause, detail)` and neither source is consulted:
    `rs_heartbeat` classifies only the turn's last record (`break_cause.classify_turn_end`), and a
    tail-wide re-read could land a different cause."""
    hook_payload = hook_payload or {}
    # A pre-resolved transcript keeps the evidence pointer on the file the verdict was read from.
    transcript = transcript or break_cause.session_transcript_path(break_cause.project_dir()) or ""
    # Last resort, confined (`_payload_transcript`).
    if not transcript:
        transcript = _payload_transcript(hook_payload)
    cause, detail = verdict if verdict is not None else break_cause.classify_transcript(transcript)
    if verdict is None and cause == break_cause.UNKNOWN:
        # The documented key is `error_type`; claude-code 2.1.285 actually sends `error` (every StopFailure
        # payload measured on a fleet node, 2026-09-30) — read both, documented spelling first.
        error_type = hook_payload.get("error_type") or hook_payload.get("error")
        mapped = _ERROR_TYPE_CAUSES.get(error_type) if isinstance(error_type, str) else None
        # A Bedrock model refusal arrives as an auth-shaped `error_type`; the payload's own last message (the
        # vendor's error, on a StopFailure) tells it apart, so the account is not benched as `auth_expired`.
        last_message = hook_payload.get("last_assistant_message")
        if mapped and isinstance(last_message, str) and break_cause.MODEL_REFUSED.search(last_message):
            mapped = break_cause.MODEL_REFUSED_CAUSE
        if mapped:
            cause = mapped
            # Prose naming its source, deliberately unlike a verbatim transcript line (which alone can be
            # re-checked against the file).
            detail = (
                f"cause from StopFailure hook payload: error_type={error_type}"
                + (f" (transcript read: {detail})" if detail else " (transcript read matched nothing)")
            )
    # The fence identity (see `_runner_session_id`); the transcript filename is the fallback.
    session_id = _runner_session_id() or (os.path.basename(transcript)[: -len(".jsonl")] if transcript else "")
    # No `node_id` or `account`: the daemon stamps both from the dispatch record and refuses caller-supplied
    # ones (a chosen `account` would let any runner condemn any account).
    payload = {
        # No `task_id`: the daemon stamps it and refuses one that is named, so runner A cannot file onto B's task.
        "cause": cause,
        "detail": detail,
        "session_id": session_id,
        "transcript": transcript,
        "observed_by": observed_by,
    }
    if extra:
        payload.update(extra)
    return payload, cause, detail


def _post_runner_break(payload: dict, cause: str, verb: str) -> bool:
    """Record the break (`session.error`), logging under the caller's `verb`: a refusal as
    `rejected`, no answer as `best-effort failed`. True when the control plane answered (took it or
    refused it — asking again changes nothing), False when it may never have arrived."""
    try:
        agent_socket.call("session.error", payload)
        _log(f"{verb} recorded runner break cause={cause}")
    except agent_socket.AgentSocketRefused as exc:
        _log(f"{verb} runner-break record rejected reason={exc.reason} (non-fatal)")
    except agent_socket.AgentSocketUnavailable:
        _log(f"{verb} runner-break record best-effort failed (non-fatal)")
        return False
    return True


def _record_break() -> tuple:
    """Classify this session's break and record it. Returns `(cause, detail, session_id)` so the caller's
    /fail carries the SAME reason and fence identity as the incident."""
    payload, cause, detail = _classify_and_build_break_payload(observed_by="runner-hook")
    _post_runner_break(payload, cause, "settle")
    return cause, detail, payload["session_id"]


def rs_stopfailure() -> int:
    """StopFailure: the turn ended on an API error while the session is ALIVE and idle — no other hook
    fires then, so the task would sit `active` forever. Classify as `settle` does and send
    `request_replace: true`. Does NOT call /fail (settle still runs afterwards).

    Single home for two payload rules: every event is LOGGED first (`_payload_summary`) — the only record
    of what the vendor said — and an event fired inside a SUBAGENT (`_is_subagent_event`) is a no-op: the
    live main agent is told its agent failed, and replacing the session kills healthy runners mid-gate."""
    if not _env_ready():
        _log("stopfailure noop=env-missing")
        return 0
    hook_payload = _hook_payload()
    # Logged before judged: stdin is the only place this payload ever exists.
    _log(f"stopfailure payload={_payload_summary(hook_payload)}")
    if _is_subagent_event(hook_payload):
        # Not a broken session; nothing is recorded. Accepted loss: an account verdict in the subagent's
        # `error_type` waits one turn, until the main thread hits the same wall. `!r` is load-bearing: a
        # lone surrogate would raise out of `_log`, and a hook exception kills the live session.
        _log(
            "stopfailure noop=subagent "
            f"agent_id={hook_payload.get('agent_id')!r} agent_type={hook_payload.get('agent_type')!r} "
            f"error_type={hook_payload.get('error_type') or hook_payload.get('error')!r} (the session is alive; the main agent is told "
            "its agent failed and decides what to do about it)"
        )
        return 0
    payload, cause, _detail = _classify_and_build_break_payload(
        observed_by="stopfailure-hook", extra={"request_replace": True},
        hook_payload=hook_payload,
    )
    _post_runner_break(payload, cause, "stopfailure")
    return 0


# Fields Claude Code adds when a hook fires inside a subagent; either non-empty marks the subagent shape.
_SUBAGENT_EVENT_KEYS = ("agent_id", "agent_type")

# Allowlisted payload fields logged by value; any other key is logged by name only.
_PAYLOAD_LOG_KEYS = ("hook_event_name", "error_type", "error", "agent_id", "agent_type", "session_id", "reason")


def _is_subagent_event(hook_payload: dict) -> bool:
    """True iff the event was fired inside a subagent (`_SUBAGENT_EVENT_KEYS`); a non-dict is False."""
    if not isinstance(hook_payload, dict):
        return False
    return any(
        isinstance(hook_payload.get(k), str) and hook_payload.get(k) for k in _SUBAGENT_EVENT_KEYS
    )


def _payload_summary(hook_payload: dict) -> str:
    """One log-line rendering of the event: `empty` when nothing was read (the stdin read missed), else
    allowlisted values plus other keys' NAMES, so a taxonomy change shows up."""
    if not isinstance(hook_payload, dict) or not hook_payload:
        return "empty"
    parts = [f"{k}={hook_payload[k]!r}" for k in _PAYLOAD_LOG_KEYS if k in hook_payload]
    extra = sorted(k for k in hook_payload if k not in _PAYLOAD_LOG_KEYS)
    if extra:
        parts.append(f"other_keys={','.join(extra)}")
    return " ".join(parts)


def rs_heartbeat() -> int:
    """Stop: 'I am still working.' Fires on every turn boundary; `session.heartbeat` is a no-op normally and
    a self-heal when a transient `StopFailure` parked the task `ready` but the session survived
    (`reassert_active_from_parked`, apps/api/app/routes/tasks/). Best-effort; a stale-session refusal
    is expected.

    A turn that ended on an account wall with a plain `Stop` (e.g. while waiting on a background agent)
    is a BREAK: the last record is classified (`break_cause.classify_turn_end`) and a cause in
    `_TURN_END_WALL_CAUSES` reports `session.error` with `request_replace` and NO heartbeat — else the
    CP would relaunch on the same walled account. The transcript comes from env, never stdin.

    The Stop payload's `background_tasks` / `session_crons` (Claude Code) say the turn ended with work in flight
    that starts the next turn itself: sent as `self_waking`, so a chat stays `active` rather than parking on the
    operator once per background notification. Absent (another vendor, a missed read): a plain Stop."""
    if not _env_ready():
        _log("heartbeat noop=env-missing")
        return 0
    session_id = _runner_session_id()
    if not session_id:
        _log("heartbeat noop=no-session-id")
        return 0
    if _wall_already_reported(session_id):
        # `wallcheck` replaced this session mid-turn; a heartbeat now would argue with that.
        _log("heartbeat noop=wall-already-reported (the replace is under way)")
        return 0
    transcript = _own_transcript(session_id)
    cause, detail = break_cause.classify_turn_end(transcript)
    if cause in _TURN_END_WALL_CAUSES:
        payload, cause, _detail = _classify_and_build_break_payload(
            observed_by="stop-hook", extra={"request_replace": True}, verdict=(cause, detail),
            transcript=transcript,
        )
        _post_runner_break(payload, cause, "heartbeat")
        _log(f"heartbeat: turn ended on an account wall (cause={cause}) — reported a break for replace "
             "instead of a heartbeat")
        return 0
    stop_event = _hook_payload()
    args: dict = {"session_id": session_id}
    if stop_event.get("background_tasks") or stop_event.get("session_crons"):
        args["self_waking"] = True
    try:
        result = agent_socket.call("session.heartbeat", args)
        # The reply is the one diagnostic; `default=str` so logging never raises inside a Stop hook.
        _log(f"heartbeat posted result={json.dumps(result, default=str)}")
    except agent_socket.AgentSocketRefused as exc:
        _log(f"heartbeat rejected reason={exc.reason} (non-fatal)")
    except agent_socket.AgentSocketUnavailable:
        _log("heartbeat best-effort failed (non-fatal)")
    return 0


def rs_wallcheck() -> int:
    """PreToolUse: replace the session the moment Claude Code's usage-limit grace note lands in this turn
    (`break_cause.turn_usage_limit_note`), rather than let the grace allowance run out and the session idle
    on its auto-continue. Files `session.error` with `request_replace` once per session (a marker in the
    hook log dir; `rs_heartbeat` honours it too); the CP ends the attempt, benches the account and kills
    this pane. Reads no stdin (the chain's guard hook owns it) and blocks nothing; one byte scan per call."""
    if not _env_ready():
        return 0
    session_id = _runner_session_id()
    if not session_id or _wall_already_reported(session_id):
        return 0
    transcript = _own_transcript(session_id)
    cause, detail = break_cause.turn_usage_limit_note(transcript)
    if cause == break_cause.UNKNOWN:
        return 0
    payload, cause, _detail = _classify_and_build_break_payload(
        observed_by="usage-limit-note-hook", extra={"request_replace": True}, verdict=(cause, detail),
        transcript=transcript,
    )
    if not _post_runner_break(payload, cause, "wallcheck"):
        return 0    # undelivered (the daemon restarting): the next tool call, or the Stop read, tries again
    _mark_wall_reported(session_id)
    _log(f"wallcheck: the usage-limit note landed in this turn (cause={cause}) — reported a break for replace")
    return 0


def _own_transcript(session_id: str) -> str:
    """This session's transcript: our own (`<proj_dir>/<session_id>.jsonl`) first, newest-by-mtime only as
    fallback — a neighbour's file must not wall this attempt. The id's shape is asserted before it becomes
    a path. `""` when there is none."""
    proj_dir = break_cause.project_dir()
    own = (os.path.join(proj_dir, f"{session_id}.jsonl")
           if re.fullmatch(r"[0-9A-Za-z-]{1,64}", session_id) else "")
    return own if own and os.path.isfile(own) else break_cause.session_transcript_path(proj_dir) or ""


def _wall_marker(session_id: str) -> str:
    """The once-per-session marker of a `wallcheck` report (`""` for an id unfit for a filename)."""
    if not re.fullmatch(r"[0-9A-Za-z-]{1,64}", session_id):
        return ""
    return os.path.join(environment.hook_log_dir(), f"wall-reported-{session_id}")


def _wall_already_reported(session_id: str) -> bool:
    marker = _wall_marker(session_id)
    return bool(marker) and os.path.exists(marker)


def _mark_wall_reported(session_id: str) -> None:
    """Written only once the CP answered the report. Best-effort: a missing marker only costs a repeat
    report, which the CP refuses as stale."""
    marker = _wall_marker(session_id)
    if not marker:
        return
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "a"):
            pass
    except OSError:
        pass


def _quit_signature(args: list[str]) -> tuple[str, str]:
    """`(reason, pane marker)` from `console_exit`'s argv — `--reason R [--pane-marker M]`, composed by
    `fleet_launch._lifecycle_chains` from the adapter's `quit_signature()`. No `--reason` = Claude's
    `_CONSOLE_EXIT_REASON` and no marker, so a chain rendered before the flags existed keeps its meaning."""
    reason, marker = _CONSOLE_EXIT_REASON, ""
    for i, arg in enumerate(args):
        if arg not in ("--reason", "--pane-marker"):
            continue
        if i + 1 >= len(args):
            _log(f"console_exit: {arg} has no value (ignored — the chain is malformed)")
            break
        if arg == "--reason":
            reason = args[i + 1]
        else:
            marker = args[i + 1]
    return reason, marker


def _pane_shows_quit(pane: str, marker: str) -> bool:
    """Does `marker` START a line in the pane's last `_QUIT_MARKER_TAIL_LINES` non-blank lines?

    Box borders and indentation are stripped and the following lines are joined on, so a marker the CLI
    boxed or wrapped on a narrow pane (a phone console narrows the window) still matches. Anchored at a
    line start and confined to the tail, so a model reply quoting the words mid-line or higher up does not."""
    lines = [" ".join(ln.strip(_BOX_CHARS + " ").split()) for ln in pane.splitlines()]
    tail = [ln for ln in lines if ln][-_QUIT_MARKER_TAIL_LINES:]
    want = " ".join(marker.split())
    return any(" ".join(tail[i:]).startswith(want) for i in range(len(tail)))


def _own_pane() -> str:
    """The visible text of the tmux pane this runner lives in, or `""` when it cannot be read (no
    `$TMUX_PANE`, no tmux, a pane already gone, the timeout). `""` fails closed: no marker, no completion."""
    pane = os.environ.get("TMUX_PANE") or ""
    if not pane:
        return ""
    try:
        done = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True,
                              text=True, timeout=_PANE_CAPTURE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def rs_console_exit(args: "list[str] | None" = None) -> int:
    """SessionEnd: the operator typed `/exit` in this task's Web Console. On an INTERACTIVE chat — a task
    whose done-criterion is `operator`, which the CP alone knows — that is the human ending the
    conversation, so the CP completes the row.

    Only this CLI's quit signature (`_quit_signature`: the payload's `reason`, plus — where the vendor's
    reason also covers other endings — a marker the CLI draws on its own pane only while quitting):
    completing frees the worktree and the runner is reaped seconds later, so every other ending is a
    logged no-op. `session_id` fences it to THIS attempt (`_runner_session_id`); no id, no call. Not
    detached (unlike `user-session-end.py`, which releases its own slot): this only POSTs, and a detached
    child would lose the pane ancestry the daemon identifies the caller by. A refusal is expected for
    non-interactive personas.
    """
    if not _env_ready():
        _log("console_exit noop=env-missing")
        return 0
    want_reason, marker = _quit_signature(args or [])
    reason = _hook_payload().get("reason")
    if reason != want_reason:
        _log(f"console_exit noop=reason-{reason or 'unset'} (only a prompt /exit ends a chat)")
        return 0
    if marker and not _pane_shows_quit(_own_pane(), marker):
        # Same reason, different ending (a new conversation in the same process, a signalled CLI).
        _log(f"console_exit noop=no-quit-marker reason={reason} (this CLI's {reason!r} also covers "
             "endings that are not a prompt quit, and its pane does not show one)")
        return 0
    session_id = _runner_session_id()
    if not session_id:
        # Fail closed: an unfenced completion is worse than none.
        _log("console_exit noop=no-session-id")
        return 0
    try:
        result = agent_socket.call("session.close", {"session_id": session_id})
        # `completed=false` is the expected superseded/already-ended case; `default=str` so logging never raises.
        _log(f"console_exit posted result={json.dumps(result, default=str)}")
    except agent_socket.AgentSocketRefused as exc:
        _log(f"console_exit refused reason={exc.reason} (non-fatal — expected off an interactive chat)")
    except agent_socket.AgentSocketUnavailable:
        _log("console_exit best-effort failed (non-fatal)")
    return 0


def rs_console_resume() -> int:
    """UserPromptSubmit: the operator sent another message, so a chat the Stop heartbeat parked
    `awaiting_response` between turns goes back to `active`.

    The only node-side signal that the operator is typing again. Fires on every prompt of every runner by
    design: the node does not know a task's persona, so the CP answers non-chats with a cheap refusal.
    `session_id` fences it to THIS attempt (`_runner_session_id`); no id, no call."""
    if not _env_ready():
        _log("console_resume noop=env-missing")
        return 0
    session_id = _runner_session_id()
    if not session_id:
        _log("console_resume noop=no-session-id")
        return 0
    try:
        result = agent_socket.call("session.resume", {"session_id": session_id})
        # The CP's outcome word is the one diagnostic; `default=str` so logging never raises.
        _log(f"console_resume posted result={json.dumps(result, default=str)}")
    except agent_socket.AgentSocketRefused as exc:
        _log(f"console_resume refused reason={exc.reason} (non-fatal — expected off an interactive chat)")
    except agent_socket.AgentSocketUnavailable:
        _log("console_resume best-effort failed (non-fatal)")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "start":
        return rs_start()
    if cmd == "settle":
        return rs_settle()
    if cmd == "stopfailure":
        return rs_stopfailure()
    if cmd == "heartbeat":
        return rs_heartbeat()
    if cmd == "wallcheck":
        return rs_wallcheck()
    if cmd == "console_exit":
        return rs_console_exit(argv[1:])
    if cmd == "console_resume":
        return rs_console_resume()
    print("usage: runner-status.py "
          "{start|settle|stopfailure|heartbeat|wallcheck|console_exit|console_resume}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
