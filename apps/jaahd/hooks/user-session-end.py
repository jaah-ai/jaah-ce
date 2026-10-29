#!/usr/bin/env python3
"""user-session-end.py — the interactive-user counterpart of runner-status.py's terminal settle: what a
`role=user` pool session (a console tab, launched via `clauded_wt.py`) reports when the human closes it.
Wired by `fleet_launch.build_settings` into the `SessionEnd` chain only for `role == "user"`.

A user session launches with `VISIBLE_AUTOCLOSE=0`, so it gets no idle ladder; `SessionEnd` is its
verified-terminal moment.

  settle  Report this session's end to the control plane, via a DETACHED child (see below).

The hook reports an EVENT, it decides nothing: the child speaks `user_session.ended` on the
daemon socket and the control plane frees the `user` hold if the `session_id` still matches. Any answer
is terminal; only "no answer" is retried. A release performs no reset. The detach is required: SessionEnd
runs while `claude` is alive, and blocking on retries would hold its exit open for a minute.
Best-effort, always exit 0, no-op off a pool worktree; logged to `<log>/hook/user-session-end.log`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent

# The daemon socket is this tokenless hook's only route to the control plane. apps/jaahd is APPENDED
# to sys.path (never inserted) so nothing here shadows a stdlib module. The import is guarded because a
# failing hook import kills the session; with no client the child logs the hold as UNRELEASED.
_JAAHD_DIR = str(_SELF_DIR.parent)
if _JAAHD_DIR not in sys.path:
    sys.path.append(_JAAHD_DIR)
# The outward-surface resolver, unguarded: no degraded mode exists, and it cannot raise at import.
import environment  # noqa: E402
from jaah_env import env_get  # noqa: E402
try:
    import agent_socket  # noqa: E402
except Exception:  # noqa: BLE001 — a hook must never crash the session it runs in
    agent_socket = None  # type: ignore[assignment]

# One socket request/reply: the daemon's own verb budget (`agent_vocabulary.Verb.timeout_s`), finite so a
# wedged daemon cannot keep the child alive forever.
_CALL_TIMEOUT = 30.0

# Retries only NO ANSWER (`AgentSocketUnavailable`); a refusal earns the same no again. No backstop exists:
# if every attempt fails the slot stays `user`-owned until an operator releases it by hand.
_RELEASE_ATTEMPTS = 10
_RELEASE_INTERVAL = 6.0

# A `/clear` ends the session object but NOT the human's occupancy of the slot: Claude Code immediately
# starts a fresh session in the same worktree. Releasing on it would free a slot somebody is sitting in.
_KEEP_HOLD_REASONS = frozenset({"clear"})

# The resolved `projects.name` the launcher exports (`fleet_launch.PROJECT_NAME_ENV`, restated: a hook
# must not import daemon code; pinned by tests on both sides). Preferred over `slot_of`, whose path
# yields the REPO name — wrong for any project whose name differs. Absent ⇒ path fallback, never a refusal.
_PROJECT_NAME_ENV = "JAAH_PROJECT_NAME"
# Its `projects.id` (`fleet_launch.PROJECT_ID_ENV`, restated likewise): the identity the report addresses the
# slot by. Absent ⇒ the report carries the name alone and the CP falls back to it.
_PROJECT_ID_ENV = "JAAH_PROJECT_ID"



def _log(*parts: str, loud: bool = False) -> None:
    """Durable decision log (instrumentation only — never gates a decision).

    `loud=True` also writes to stderr, for a manual run or a wrapper that captures it."""
    log_dir = environment.hook_log_dir()
    line = ' '.join(parts)
    if loud:
        try:
            print(line, file=sys.stderr)
        except OSError:
            pass
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(log_dir / "user-session-end.log", "a") as f:
            f.write(f"{stamp} {line}\n")
    except OSError:
        pass


def slot_of(worktree: str) -> tuple[str, str] | None:
    """`(project, number)` for a pool worktree, else None (a main clone or an ad-hoc directory — nothing
    to report).

    Deliberately not `agent_config_dirs.slot_of` (see its docstring): a hook must not import daemon code.
    The project half is only a fallback to `$JAAH_PROJECT_NAME`; the number and the is-a-slot verdict
    are authoritative."""
    path = worktree.rstrip("/")
    base = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    if parent and base and base.isdigit():
        return (parent, base)
    if "-" not in base:
        return None
    project, number = base.rsplit("-", 1)
    if not project or not number or not number.isdigit():
        return None
    return (project, number)


def _after_exit(project: str, number: str, session_id: str = "", project_id: str = "") -> int:
    """The detached half: report this session's end to the control plane, retrying only a missing answer a
    bounded number of times.

    `session_id` makes it staleness-safe: a late report on a re-claimed slot is answered `stale`.
    Any answer is terminal (`already free`, `task-held`, `no such slot`, `stale`); `AgentSocketRefused` is
    terminal and logged loudly as UNRELEASED; only `AgentSocketUnavailable` is retried. Nothing else ever
    frees a permanently unanswered hold, so that too is logged loudly (an `UNRELEASED` marker to grep)."""
    if agent_socket is None:
        _log(
            f"UNRELEASED: settle {project}#{number} has no socket client (agent_socket failed to import) "
            "— the hold stays user-owned until an operator releases it by hand",
            loud=True,
        )
        return 0
    try:
        slot_number = int(number)
    except ValueError:
        _log(f"UNRELEASED: settle {project}#{number} is not a slot number", loud=True)
        return 0
    for attempt in range(1, _RELEASE_ATTEMPTS + 1):
        try:
            # A literal dict on purpose: `tests/test_verb_call_contracts.py` derives call shapes from source.
            answer = agent_socket.call(
                "user_session.ended",
                {"project": project, "project_id": int(project_id) if project_id.isdigit() else None,
                 "number": slot_number, "session_id": session_id},
                timeout=_CALL_TIMEOUT,
            )
        except agent_socket.AgentSocketRefused as exc:
            _log(
                f"UNRELEASED: settle {project}#{number} refused ({exc.reason}) attempt={attempt} — the "
                "daemon or the control plane said no and a retry earns the same answer; the hold stays "
                "user-owned until an operator releases it by hand",
                loud=True,
            )
            return 0
        except agent_socket.AgentSocketUnavailable as exc:
            _log(f"settle {project}#{number} unanswered attempt={attempt} err={exc}")
            if attempt < _RELEASE_ATTEMPTS:
                time.sleep(_RELEASE_INTERVAL)
            continue
        released = answer.get("released") if isinstance(answer, dict) else None
        reason = answer.get("reason") if isinstance(answer, dict) else answer
        _log(f"settle {project}#{number} reported attempt={attempt} released={released} reason={reason}")
        return 0
    _log(
        f"UNRELEASED: settle {project}#{number} went unanswered after {_RELEASE_ATTEMPTS} attempts — "
        "no reaper will free this slot; it stays user-owned until an operator releases it by hand "
        "(`ctl.py release`, or the equivalent console action)",
        loud=True,
    )
    return 0


def settle(worktree: str, reason: str, session_id: str = "") -> int:
    """The SessionEnd entry point: hand the report to a detached `_after_exit` child.

    `slot_of` decides whether this is a pool worktree and its number; the project name comes from
    `$JAAH_PROJECT_NAME`, else the path. The log records which source was used."""
    slot = slot_of(worktree)
    if slot is None:
        _log(f"settle noop=not-a-pool-worktree wt={worktree or '<unset>'}")
        return 0
    number = slot[1]
    injected = (env_get(_PROJECT_NAME_ENV) or "").strip()
    project = injected or slot[0]
    source = "env" if injected else "path"
    project_id = (env_get(_PROJECT_ID_ENV) or "").strip()
    if reason in _KEEP_HOLD_REASONS:
        _log(f"settle {project}#{number} noop=reason-{reason} (session restarts in place)")
        return 0

    _log(
        f"settle {project}#{number} project-from={source} reason={reason or 'unset'} "
        f"→ detaching report"
    )
    # Detach fully (stdio to devnull, new session) so the child outlives this hook and the session. Windows
    # ignores `start_new_session`: its own process group keeps the hook console's Ctrl+C off the child there.
    posix = hasattr(os, "setsid")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_after_exit", project, number, session_id, project_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=posix,
        creationflags=0 if posix else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return 0


def _hook_payload() -> dict:
    """The SessionEnd payload on stdin, or {} when stdin carries nothing usable."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "settle":
        worktree = ""
        rest = argv[1:]
        if rest[:1] == ["--worktree"] and len(rest) > 1:
            worktree = rest[1]
        payload = _hook_payload()
        # A missing `reason` is treated as a real end: the field is instrumentation for ONE case
        # (`clear`), and its absence must not silently skip the teardown.
        return settle(
            worktree or os.environ.get("PWD", os.getcwd()),
            str(payload.get("reason") or ""),
            str(payload.get("session_id") or ""),
        )
    if cmd == "_after_exit":
        # Internal verb only: the detached child `settle` spawns.
        if len(argv) < 3:
            return 0
        return _after_exit(argv[1], argv[2], argv[3] if len(argv) > 3 else "", argv[4] if len(argv) > 4 else "")
    print("usage: user-session-end.py settle [--worktree <dir>]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
