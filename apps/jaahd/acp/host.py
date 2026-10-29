"""acp/host.py — `jaah-acp-host --launch <launch.json>`: one host per run, the pane root (D-HOST-1).

The host spawns the engine as its stdio child, speaks ACP to it (`client.py`), normalises what it says
into schema-v1 events (`events.py`) shipped to the daemon in batches (`runtime.events_append`), answers
its requests (`requests.py`), and takes operator intents off the control lane (`runtime.control_wait`).
Every daemon call goes through the local vocabulary socket; this process holds no credential.

D-SUP-1 supervision, the rules that decide when an attempt ends:
  * after each prompt result, `task.get`: a task no longer `active` -> close the session, stop the engine,
    exit 0;
  * an open user_input request, or a task `awaiting_response` after a turn (a question the agent raised with
    `input.request` itself) -> no nudge and no autoclose; wait for intents;
  * `end_turn` on an open task -> one nudge prompt; after the nudge, `idle_linger_s` without an intent
    ends the attempt;
  * a task state the CP treats as finished or re-dispatchable (`_CLOSED_STATES`) closes it; any other answer
    (`awaiting_response` included) keeps it open;
  * the engine exiting, or its stdout closing (any time), ends the attempt; a vendor event never does;
  * a prompt error classed `usage_limit` or `auth` (the first one: a refused account fails every turn), or one the
    engine names a break cause for (its dialect's error kinds and
    wording, a vendor's refusal of the account: `events.error_cause`; on `session/new` too) -> `session.error`
    with `request_replace` (retried; if it never lands the attempt ends at once and `end_attempt` records it),
    posted ONCE per attempt — today's self-heal, break + replace;
  * a close intent cancels the turn and waits `CLOSE_GRACE_S` for the prompt to return, then stops the engine;
  * a close intent carrying `handoff: true` (the console toggling the slot to its terminal front,
    `reconcile.enact_launch._launch_into_slot`) is the outcome `handoff`: session/close, engine stop, and NO `end_attempt` —
    the task stays `active` because another front opens the same native session next;
  * a resumed session (`session/resume` or `session/load` opened it) sends no first prompt and waits for the
    operator with no idle deadline until a turn has run: the conversation is already under way;
  * an exception in the turn loop is the outcome `host_error`, ended like any other.
Ending an attempt is `attempt_end.end_attempt`, the same ladder the runner-status hook runs.

An SDK v2 run (`runtime.run_dir`) owns its attempt's run dir: the host removes it once the attempt has ended and the
engine has stopped (`remove_run_dir`, also on SIGTERM/SIGHUP), never while it runs, and stays the pane root
throughout. A registry install (`runtime.install`) is built into that run dir first, as this process — the pane's
session user (`acp_registry_install.install`) — and its result is the engine; a bundle's declared dependencies
(`runtime.deps`) are installed beside its script the same way before its command starts
(`acp_registry_install.install_bundle_dependencies`); a failed install is the attempt's `task.fail`, named. Under the `acp-core` profile the host is a plain ACP client: no `jaah.ai` extension methods are
advertised, and the agent settles through the control MCP server. Its key route `acp_authenticate` puts no key in
the engine's environment (the env fences below refuse one either way): when the launch names a bound account
(`runtime.key_account`), the host reads that account's key from the node broker — which serves it to the pane's root
alone — and sends it in ACP `authenticate` between `initialize` and the session (`_authenticate`). A model runner's
API-key launch (launch.json `authenticate`) signs in the same way, its key the daemon's one
`runtime.account_key` answer for the runner's leased account, fenced to the pane's root. The key lives in
this process's memory for that one request: never in the engine's env, launch.json, an event or a log line. A
failure (no API-key method, an unreadable key, a refused sign-in) ends the attempt as a failed session start, named.

Events never vanish silently: batches are capped by count AND serialized size (the broker's request line is
bounded); a batch the daemon refuses as `bad_request` is halved until the one refused event is replaced by
a content-free `error` placeholder; a batch never acknowledged is replaced by one after `BATCH_MAX_FLUSHES`.

Dependency-injected (daemon client, clock, sleep, spawn, process scan) so a replay harness can drive it
with a fake engine and an in-process daemon stub. Runnable as a script: apps/jaahd is APPENDED to
sys.path (never inserted, as `hooks/runner-status.py` does), so nothing beside this file shadows stdlib.
"""
from __future__ import annotations

import asyncio
import collections
import json
import ntpath
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_JAAHD_DIR = str(Path(__file__).resolve().parent.parent)
if _JAAHD_DIR not in sys.path:
    sys.path.append(_JAAHD_DIR)

import agent_providers  # noqa: E402
import agent_socket  # noqa: E402
import attempt_end  # noqa: E402
from acp import client as acp_client  # noqa: E402
from acp import events, jsonrpc, launch, lifecycle, modes, requests, selfcheck  # noqa: E402

READER_LIMIT = 64 * 1024 * 1024
WAIT_BUDGET_S = 55.0  # socket budget for the daemon's 50 s long-polls
CALL_BUDGET_S = 5.0
BATCH_MAX = 64
# Per `runtime.events_append` call, serialized as the socket sends it — under the broker's 256 KiB line.
BATCH_MAX_BYTES = 200 * 1024
BATCH_INTERVAL_S = 0.5
# Flushes an unacknowledged batch survives before it is replaced by a placeholder (never blocks the queue).
BATCH_MAX_FLUSHES = 3
DEFAULT_IDLE_LINGER_S = 1800.0
# How often an idle host re-reads the task's state: a task turned `awaiting_response` (or closed) mid-linger is
# seen within this, so a pending question never idles the session out.
TASK_POLL_S = 60.0
DEFAULT_NUDGE = ("Your last turn ended but the task is still open. Finish the remaining work, then settle "
                 "the task (task_complete) or report why it cannot be finished (task_fail).")
STOP_GRACE_S = 5.0
#: A SIGTERM/SIGHUP (a stopped pane) first ships the events already taken — a closed turn's usage among them — for
#: at most this long, then unwinds (`Host.serve`).
SIGNAL_FLUSH_S = 3.0
#: An engine's launch wrapper, never the engine: its `--version` would label the session as `env` or `bash`.
_LAUNCH_WRAPPERS = frozenset({"env", "bash", "sh", "dash", "zsh", "sudo", "setpriv", "nice", "timeout"})
CLOSE_GRACE_S = 30.0  # real seconds a close intent waits for the cancelled prompt to return
READER_EXIT_GRACE_S = 2.0  # stdout closed: real seconds to let the exit watcher name the outcome
BREAK_RETRIES = 3
_BACKOFF_MAX_S = 30.0
_RETRYABLE = ("busy", "unavailable")
_VERB_PREFIX = "_jaah.ai/"
_OBSERVED_BY = "acp-host"
# Last error class -> `break_cause.CAUSES` (hooks/break_cause.py).
_BREAK_CAUSE = {"usage_limit": "usage_limit", "auth": "auth_expired", "api": "api_error"}
# Error classes whose first prompt error breaks the session and asks for another account at once, named cause or
# not: a refused account fails every later turn too, and re-dispatch must skip it (`auth_expired` writes the account's
# `auth_rejected` verdict, which the scheduler excludes).
_REPLACE_CLASSES = frozenset({"usage_limit", "auth"})
#: The break cause of an engine that never ran a turn (no process, or no session), when its error names none.
_START_CAUSE = "crashed"
NATIVE_REF_KEY = launch.NATIVE_REF_KEY  # re-exported: the launch writer and this reader share one spelling
#: The dev check's fault flag (architecture C14): `api_error` fails the first prompt with a server-error frame
#: (`FAULT_FRAME`). Honoured only in a test build (`fault_armed`).
FAULT_ENV = "JAAH_ACP_FAULT"
# Set in the engine's env (so in its hooks'): this host files the engine's questions from their ACP elicitation
# (`_elicit`), so a question hook in the engine must not file the same round again
# (the node's question-capture hook reads this name).
ACP_HOST_ENV = "JAAH_ACP_HOST"
# Set in the engine's env when the run has a files dir (`Host.files_dir`): the context spool the in-band dispatch
# appends to (the hook dispatcher's `HOOK_CONTEXT_ENV`, which a test pins equal), drained before each prompt.
HOOK_CONTEXT_ENV = "JAAH_HOOK_CONTEXT"
HOOK_CONTEXT_FILE = "hook-context.jsonl"
# The system prompt file written for an engine whose dialect names `system_prompt_file_env` (`_write_system_prompt_file`).
SYSTEM_PROMPT_FILE = "system-prompt.md"
#: The heading of the leading text block that carries hook context into a prompt (`Host._drain_hook_context`).
GUIDANCE_HEADING = "Platform guidance:"
#: Consecutive turns a `Stop` hook's refusal may send the agent back for (the `stop_hook_active` loop guard is the
#: hook's own; this caps a hook that never lets go).
MAX_STOP_CONTINUATIONS = 3
FAULT_API_ERROR = "api_error"
#: An engine's API-error frame for a server answering 500, its cause named in Jaah's own `_meta` namespace
#: (`events.error_cause`) so it reads the same under every provider's dialect.
FAULT_FRAME = (-32603, "Internal error: API Error: 500 Internal server error. This is a server-side issue, usually "
                       "temporary \u2014 try again in a moment.",
               {"_meta": {events.META_KEY: {"error_class": "api", "cause": "api_error"}}})
# Task states that end the run: finished, or handed back for (re)dispatch. `paused` too — the CP stops
# every runner of a paused task (`task_lifecycle._NO_RUNNER_STATES`).
_CLOSED_STATES = frozenset({"done", "system_failed", "ready", "draft", "paused"})
#: The state of a task parked on an operator question: no nudge and no idle end, as for an open user_input request.
AWAITING_STATE = "awaiting_response"
_BAD_REQUEST = "bad_request"
_TOO_LARGE = "event too large to record"
_UNACKED = "events not recorded: the daemon never acknowledged them"

#: `runtime.profile` of a plain ACP agent (`apps/api/app/acp_registry.py::PROFILE`): no extension advertised.
PROFILE_ACP_CORE = "acp-core"
#: The code a failed registry install is reported with (`task.fail`).
INSTALL_FAILED = "[agent_install_failed]"
#: The code an SDK run's refused launch env is reported with (`task.fail`).
LAUNCH_REFUSED = "[agent_env_refused]"
#: `runtime.key_delivery` of a run whose key goes in ACP `authenticate` (`apps/api/app/acp_registry.py::KEY_DELIVERY`).
KEY_DELIVERY_AUTHENTICATE = "acp_authenticate"
#: The broker's name for an API-key account's key (`account_auth.NODE_CREDENTIAL_PREFIX`).
ACCOUNT_KEY_PREFIX = "account_api_key:"
#: The key read's budget: on a cold cache the broker fetches it from the CP, which can be slow.
KEY_FETCH_S = 30.0
#: The code a failed sign-in is reported with (the session-start error event).
AUTH_FAILED = "[agent_auth_failed]"
#: How long a failure report waits for the launcher's claim (`agent_process.CLAIM_POLLS` × 0.5 s).
CLAIM_WAIT_S = 60.0
_CLAIMED_STATES = frozenset({"active", AWAITING_STATE})

# Turn-loop outcomes.
END_TASK_CLOSED = "task_closed"
END_IDLE = "idle_autoclose"
END_CLOSE_INTENT = "close_intent"
END_HANDOFF = "handoff"  # a close handing the native session to another front: the task stays active
END_INIT_FAILED = "init_failed"
END_SPAWN_FAILED = "spawn_failed"
END_HOST_ERROR = "host_error"
END_CONNECTION_CLOSED = "connection_closed"
END_BREAK_UNRECORDED = "break_unrecorded"
END_BREAK_RECORDED = "break_recorded"  # the replace-requesting break posted: the account cannot run, nothing to linger for
END_AGENT_ERROR = "agent_error"  # a prompt the engine failed, with no replacement asked for: the attempt ends now

#: The file an engine's own wrapper may send its stderr to, for an engine that discards a child's stderr (its
#: crash would reach the host only as the adapter's own write failure). The host names a fresh file per run in the
#: engine's env; when the engine dies before a session opens, the file's tail is the attempt's last error, so the
#: break names the real cause (the adapter whose wrapper writes there reads this name).
ENGINE_STDERR_ENV = "JAAH_ENGINE_STDERR"
#: How much of that file's end rides the error event (it is redacted like any other message).
ENGINE_STDERR_TAIL = 1500


def is_test_build() -> bool:
    """This host runs from a source checkout (the replay rig, a `./dc` stack, a pool worktree), never from an
    installed package: only then may a fault flag change what a run does."""
    return any((parent / ".git").exists() for parent in Path(__file__).resolve().parents)


def fault_armed(environ) -> bool:
    """`JAAH_ACP_FAULT=api_error` in a test build. Anywhere else the flag is ignored, loudly."""
    if environ.get(FAULT_ENV) != FAULT_API_ERROR:
        return False
    if not is_test_build():
        _log(f"{FAULT_ENV} ignored: this host is an installed build")
        return False
    return True


def _pricing_tier(provider: str):
    """The provider adapter's `line_pricing_tier`, or None (no adapter by that name, or no tier)."""
    try:
        import agents  # noqa: PLC0415 — the adapter registry, only to look the one member up

        return getattr(agents.get_adapter(provider), "line_pricing_tier", None)
    except Exception:  # noqa: BLE001 — an unknown provider (a replay rig) bills at the base rate
        return None


def _install(install: dict) -> tuple[list, dict]:
    """The default installer: `acp_registry_install.install` into `install["dest"]`, as this process's user."""
    import acp_registry_install  # noqa: PLC0415 — only a registry run installs

    installed = acp_registry_install.install(
        {"pin": install["pin"], "agent": install.get("agent") or {}}, slug=install["slug"], org_id=install["org_id"],
        dest=Path(install["dest"]))
    return list(installed.argv), dict(installed.env)


def _install_deps(deps: dict) -> None:
    """The default dependency installer: `acp_registry_install.install_bundle_dependencies` beside the bundle's
    script, as this process's user."""
    import acp_registry_install  # noqa: PLC0415 — only an SDK bundle run installs

    acp_registry_install.install_bundle_dependencies(Path(deps["dest"]), deps["command"], name=deps["name"])


def remove_run_dir(spec: launch.LaunchSpec) -> None:
    """Remove an SDK v2 attempt's run dir (`runtime.run_dir`) once the attempt is over. Only a dir shaped like
    this task's own (`agent-runs/task-<task_id>-<hex>`, `agent_process.new_run_dir`) is touched; a missing one is
    fine. Best effort: a failure is logged, never raised."""
    run_dir = str(spec.runtime.get("run_dir") or "")
    if not run_dir:
        return
    path = Path(run_dir)
    if path.parent.name != "agent-runs" or not re.fullmatch(rf"task-{re.escape(spec.task_id)}-[0-9a-f]+", path.name):
        _log(f"run dir {run_dir} is not this task's; left in place")
        return
    def writable_and_retry(func, target, _exc) -> None:
        # An unpacked archive or a package tree can hold a directory with no write bit: open it, then retry.
        os.chmod(os.path.dirname(target) or target, 0o700)
        func(target)

    try:
        shutil.rmtree(path, onexc=writable_and_retry)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log(f"run dir {run_dir} not removed: {type(exc).__name__}")


def _exit_on_signal(signum, _frame) -> None:
    """SIGTERM/SIGHUP (a killed pane) unwind through `main`'s cleanup instead of dying in place."""
    raise SystemExit(128 + signum)


def _input_includes_cached(provider: str) -> bool:
    """The provider's `Usage.input_includes_cached`; False for a value this build does not know (a replay rig)."""
    caps = agent_providers.capabilities_of(provider)
    return caps.usage.input_includes_cached if caps is not None else False


async def _fail_with(exc: BaseException) -> dict:
    raise exc


def _log(line: str) -> None:
    print(f"jaah-acp-host: {line}", file=sys.stderr, flush=True)


def _reason(exc: agent_socket.AgentSocketRefused) -> str:
    return str(exc.reason).split(":", 1)[0]


#: The MicroVM guest's launch share (`microvm_seed.FIXED_MOUNTS["launch"]`): present only inside a guest.
GUEST_LAUNCH_DIR = "/jaah/launch"


class SocketDaemon:
    """The default daemon client: the vocabulary socket (`agent_socket.call`). In a MicroVM it carries the host's
    runtime capability (`claim`) on every call; nothing but this process's memory ever holds it."""

    def __init__(self) -> None:
        self._capability = ""

    def call(self, verb: str, args: dict, timeout: float) -> object:
        return agent_socket.call(verb, args, timeout=timeout, capability=self._capability)

    def claim(self, session_id: str) -> None:
        """Claim the host's runtime capability (`runtime.host_claim`) BEFORE the engine starts: once claimed, no
        process the agent runs can claim it, and in a VM every `runtime.*` call without it is refused. A held
        capability makes this process non-dumpable, so a same-uid child cannot read it from /proc."""
        result = agent_socket.call("runtime.host_claim", {"session_id": session_id}, timeout=CALL_BUDGET_S)
        capability = result.get("capability") if isinstance(result, dict) else ""
        if isinstance(capability, str) and capability:
            _make_undumpable()
            self._capability = capability
        elif os.path.isdir(GUEST_LAUNCH_DIR):
            raise agent_socket.AgentSocketRefused("runtime.host_claim answered no capability in a MicroVM guest")


#: How much of an engine's refusal text a sign-in failure quotes.
_ENGINE_TEXT_MAX = 300
_KEY_RUN = 8


def _engine_argv0(argv) -> list[str]:
    """The argv entry whose `--version` names the engine: its first, unless that is a launch wrapper (`env -C dir
    -- engine`, `bash -c script`), whose version would label the session as the wrapper — then none is probed and
    the label is the agent's own (`agentInfo`)."""
    if not argv or os.path.basename(str(argv[0])) in _LAUNCH_WRAPPERS:
        return []
    return [argv[0]]


def _shares_key(text: str, key: str) -> bool:
    """Whether `text` carries any `_KEY_RUN`-character run of `key`."""
    return any(key[i:i + _KEY_RUN] in text for i in range(max(1, len(key) - _KEY_RUN + 1)))


def _with_meta_path(meta: dict, path: tuple[str, ...], value: str) -> dict:
    """`meta` with `value` at the nested `path`, each object on the way copied, never mutated, and a non-object
    there replaced."""
    if not path:
        return meta
    head, *rest = path
    inner = meta.get(head)
    return {**meta, head: _with_meta_path(inner if isinstance(inner, dict) else {}, tuple(rest), value)
            if rest else value}


def _make_undumpable() -> None:
    """`prctl(PR_SET_DUMPABLE, 0)`: /proc/<pid>/{mem,environ,fd} turn root's and ptrace needs CAP_SYS_PTRACE."""
    if not sys.platform.startswith("linux"):
        return
    import ctypes  # noqa: PLC0415 — Linux only, once per run

    if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:  # 4 = PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) failed")


def _engine_stderr_file(log) -> "tuple[int, str] | None":
    """A fresh, private `ENGINE_STDERR_ENV` file for this run as `(fd, path)`, or None (logged) when none can be
    made. The host keeps the fd and reads only through it: the engine (same uid) can replace what is at the path,
    never the file this fd holds."""
    import tempfile  # noqa: PLC0415 — only here

    try:
        return tempfile.mkstemp(prefix="jaah-engine-stderr-", suffix=".log")
    except OSError as exc:
        log(f"no engine stderr relay file ({exc}); a wrapper's start failure will not be named")
        return None


def _release_relay(relay: "tuple[int, str] | None", *, read: bool) -> str:
    """Remove the relay file (only while its path still names this fd's file) and close the fd; with `read`, first
    the file's last `ENGINE_STDERR_TAIL` characters, redacted BEFORE the cut (a key split by the cut would no longer
    match the redaction), whitespace-folded. "" for none."""
    if relay is None:
        return ""
    fd, path = relay
    text = ""
    try:
        st = os.fstat(fd)
        if read:
            start = max(0, st.st_size - 2 * ENGINE_STDERR_TAIL)
            raw = os.pread(fd, 2 * ENGINE_STDERR_TAIL, start).decode("utf-8", "replace")
            text = " ".join(events.redact(raw).split())[-ENGINE_STDERR_TAIL:]
        try:
            there = os.stat(path, follow_symlinks=False)
            if (there.st_dev, there.st_ino) == (st.st_dev, st.st_ino):
                os.unlink(path)
        except OSError:
            pass
    except OSError:
        pass
    finally:
        os.close(fd)
    return text


#: Where the engine's stderr names a settings object: `--settings` and what follows it on the line, in any spelling
#: an argv echo takes (`--settings {…}`, `--settings=…`, `"--settings", "{…}"`).
_SETTINGS_ARG = re.compile(r"""(--settings["']?[ \t]*[,=]?[ \t]*)[^\r\n]*""")
SETTINGS_WITHHELD = "<settings withheld>"
#: A settings key no other line carries, withheld wherever it shows (an object echoed without its flag).
_SETTINGS_KEYS = ("awsCredentialExport", "apiKeyHelper")
#: The most lines one withheld object may take: a truncated or string-braced object never hides the rest of the run.
_WITHHELD_MAX_LINES = 200


class SettingsScrubber:
    """Engine stderr, line by line, with every settings object withheld.

    An agent SDK hands the session's settings (the hook chains, `apiKeyHelper`, a Jaah account's
    `awsCredentialExport`) to the CLI it spawns as `--settings <json>`, and echoes that argv to stderr when the CLI
    fails; the pane is no place for it. A pretty-printed object spanning lines is withheld to its closing brace."""

    def __init__(self) -> None:
        self._depth = self._withheld = 0
        # A bare `--settings` ended its line: its value is the next non-empty line (`util.inspect`'s argv layout).
        self._value_next = False

    def feed(self, line: str) -> "str | None":
        """The line to show, or None when it is all inside a withheld object."""
        if self._value_next and line.strip():
            self._value_next = False
            self._open(line)
            return None
        if self._depth > 0:
            self._withheld += 1
            self._depth = max(0, self._depth + line.count("{") - line.count("}"))
            if self._withheld >= _WITHHELD_MAX_LINES:
                self._depth = 0
            return None
        match = _SETTINGS_ARG.search(line)
        if match is None:
            return None if any(key in line for key in _SETTINGS_KEYS) else line
        rest = line[match.end(1):]
        # The flag closes its line as an argv list element (`'--settings',`) or bare: its value is the next line.
        # Quoted mid-sentence (`unknown option '--settings'`) it has none.
        flag = match.group(1).rstrip(" \t")
        if not rest.strip(" \t\r\n'\"") and ("," in flag or flag == "--settings"):
            self._value_next = True
        self._open(rest)
        return line[:match.end(1)] + SETTINGS_WITHHELD + ("\n" if line.endswith("\n") else "")

    def _open(self, text: str) -> None:
        self._depth, self._withheld = max(0, text.count("{") - text.count("}")), 0


async def _relay_stderr(stream, out=None) -> None:
    """Copy the engine's stderr to the pane (this host's own stderr) through a `SettingsScrubber`, every line
    redacted (`events.redact`, as the relay file's tail is). Drains to the end whatever the pane does: a stalled
    reader would block the engine on its next stderr write."""
    scrubber = SettingsScrubber()
    while True:
        try:
            raw = await stream.readline()
        except ValueError:  # a line past the reader's limit, already discarded: keep draining
            continue
        except OSError:  # the pipe is gone with the engine
            return
        if not raw:
            return
        shown = scrubber.feed(raw.decode("utf-8", "replace"))
        if shown is None:
            continue
        try:
            target = out or sys.stderr
            target.write(events.redact(shown))
            target.flush()
        except (OSError, ValueError):  # the pane is gone: drop the line, keep draining
            pass


#: How long an ending attempt waits for the engine's stderr to drain into the pane (a live engine never closes it).
STDERR_DRAIN_S = 2.0
#: The live stderr relays, held so the loop cannot garbage-collect one while its engine runs.
_stderr_relays: set = set()


#: `taskkill`'s budget when an engine is stopped on Windows.
KILL_TREE_TIMEOUT_S = 10.0
#: The Windows switch that keeps a bare-name search (CreateProcess's, `shutil.which`'s, cmd.exe's) out of the
#: current directory.
NO_CWD_SEARCH = "NoDefaultCurrentDirectoryInExePath"


async def default_spawn(argv, *, cwd: str, env: dict):
    """The engine: stdin/stdout piped for ACP, stderr relayed to the pane with its settings withheld
    (`SettingsScrubber`).

    On Windows CreateProcess finds only an `.exe` by a bare name, and the adapter is a `.cmd` shim
    (`runner_cli_pins`' Windows bins): argv[0] is resolved on the engine's own PATH first, PATHEXT and all. Never
    against the current directory, which Windows searches first and which is the worktree — a repo committing an
    engine-named `.cmd` would run before every guard: `NoDefaultCurrentDirectoryInExePath` turns that search
    off for `which` here and for the shim's `cmd.exe`, and a name that resolves nowhere else is refused."""
    if os.name == "nt":
        env = {**env, NO_CWD_SEARCH: "1"}
        os.environ[NO_CWD_SEARCH] = "1"   # read by `shutil.which` through `_winapi.NeedCurrentDirectoryForExePath`
        resolved = shutil.which(argv[0], path=env.get("PATH"))
        if not resolved or not ntpath.isabs(resolved):
            raise FileNotFoundError(f"{argv[0]} is not on the engine's PATH")
        argv = [resolved, *argv[1:]]
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=READER_LIMIT)
    relay = asyncio.ensure_future(_relay_stderr(proc.stderr))
    _stderr_relays.add(relay)
    relay.add_done_callback(_stderr_relays.discard)
    proc.jaah_stderr_relay = relay  # type: ignore[attr-defined]  # our own tag on the Process; awaited at the attempt's end (`_drain_stderr`): the last lines name the exit
    return proc


def _kill_tree(pid: int) -> None:
    """Windows: end `pid` and every process it started (`taskkill /T /F`). Best effort: the caller's terminate and
    kill follow either way."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=KILL_TREE_TIMEOUT_S,
                       check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _has_exited(pid: int) -> bool:
    """Whether the engine `pid` has exited, asked WITHOUT reaping it (`WNOWAIT`): the loop's exit watcher still owns
    the reap, and with it the exit code. False where the platform has no `waitid` (the grace alone decides there)."""
    if not hasattr(os, "waitid"):
        return False
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True  # already reaped: the watcher has the exit in hand


def _wire_size(ev: dict) -> int:
    """An event's size as `agent_socket` serializes it (ASCII-escaped JSON)."""
    return len(json.dumps(ev, separators=(",", ":")))


def _placeholder(ev: dict, message: str) -> dict:
    """A content-free `error` event in `ev`'s place: same envelope, none of its data."""
    return {**ev, "kind": "error", "status": "failed", "data": {"class": "agent", "message": message}}


def _is_placeholder(evs: list[dict]) -> bool:
    return len(evs) == 1 and evs[0].get("kind") == "error" and (evs[0].get("data") or {}).get(
        "message") in (_TOO_LARGE, _UNACKED)


def _reset_clock(resets_at: str) -> str:
    """An ISO-8601 reset time as `resets Oct 18, 3:05pm (UTC)`, or "" when it does not parse."""
    try:
        when = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return ""
    hour = when.hour % 12 or 12
    return f"resets {when:%b} {when.day}, {hour}:{when:%M}{'am' if when.hour < 12 else 'pm'} (UTC)"


class _Batch:
    def __init__(self, events: list[dict]) -> None:
        self.id = uuid.uuid4().hex
        self.events = events
        self.flushes = 0


class EventBatcher:
    """Events -> `runtime.events_append`, flushed at `BATCH_MAX` events / `BATCH_MAX_BYTES`, or
    `BATCH_INTERVAL_S` after the first unflushed one. A batch keeps its `batch_id` across retries, so the
    daemon dedups a replay. Nothing is dropped without a log line, and content is replaced, never cut."""

    def __init__(self, host: "Host") -> None:
        self._host = host
        self._buf: collections.deque[tuple[dict, int]] = collections.deque()
        self._buf_bytes = 0
        self._queue: list[_Batch] = []
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None

    def add(self, evs: list[dict]) -> None:
        if not evs:
            return
        for ev in evs:
            size = _wire_size(ev)
            self._buf.append((ev, size))
            self._buf_bytes += size
        if len(self._buf) >= BATCH_MAX or self._buf_bytes >= BATCH_MAX_BYTES:
            self._host._spawn_task(self.flush())
        elif self._timer is None or self._timer.done():
            self._timer = self._host._spawn_task(self._flush_later())

    async def _flush_later(self) -> None:
        await self._host.sleep(BATCH_INTERVAL_S)
        await self.flush()

    def _take(self) -> _Batch:
        """The next batch off the buffer: at least one event, then up to the count and size caps."""
        events: list[dict] = []
        total = 0
        while self._buf and len(events) < BATCH_MAX:
            ev, size = self._buf[0]
            if events and total + size > BATCH_MAX_BYTES:
                break
            self._buf.popleft()
            self._buf_bytes -= size
            events.append(ev)
            total += size
        return _Batch(events)

    async def _ship(self, batch: _Batch, attempts: int) -> str:
        """"ok" | "refused" (bad_request) | "dropped" (another refusal) | "unshipped"."""
        args = {"session_id": self._host.spec.session_id, "events": batch.events, "batch_id": batch.id}
        for attempt in range(attempts):
            try:
                await self._host.call("runtime.events_append", args, CALL_BUDGET_S)
                return "ok"
            except agent_socket.AgentSocketRefused as exc:
                reason = _reason(exc)
                if reason == _BAD_REQUEST:
                    return "refused"
                if reason not in _RETRYABLE:
                    # Never accepted as sent; retrying unchanged earns the same refusal.
                    self._host.log(f"events_append refused reason={exc.reason}; "
                                   f"dropping batch of {len(batch.events)}")
                    return "dropped"
            except agent_socket.AgentSocketUnavailable:
                pass
            if attempt + 1 < attempts:
                await self._host.backoff(attempt)
        return "unshipped"

    def _split(self, batch: _Batch) -> list[_Batch]:
        """A `bad_request` batch: halves; one refused event becomes a placeholder; a refused placeholder
        is dropped with a log line (nothing smaller is left to record)."""
        evs = batch.events
        if len(evs) > 1:
            mid = len(evs) // 2
            return [_Batch(evs[:mid]), _Batch(evs[mid:])]
        if _is_placeholder(evs):
            self._host.log("events_append refused the placeholder itself; one event not recorded")
            return []
        self._host.log(f"events_append refused one {evs[0].get('kind')} event; recorded as a placeholder")
        return [_Batch([_placeholder(evs[0], _TOO_LARGE)])]

    async def flush(self, *, attempts: int = 8) -> bool:
        """Ship everything buffered. False when a batch is still unshipped after `attempts` tries (it stays
        queued, same `batch_id`, for the next flush — at most `BATCH_MAX_FLUSHES` flushes, then it is
        replaced by one placeholder so the queue behind it moves)."""
        async with self._lock:
            while self._queue or self._buf:
                if not self._queue:
                    self._queue.append(self._take())
                batch = self._queue[0]
                outcome = await self._ship(batch, attempts)
                if outcome == "unshipped":
                    batch.flushes += 1
                    if batch.flushes < BATCH_MAX_FLUSHES:
                        self._host.log(f"events_append unshipped after {attempts} tries; kept for the next flush")
                    elif _is_placeholder(batch.events):
                        self._host.log("events_append never acknowledged the placeholder; dropped")
                        self._queue.pop(0)
                    else:
                        self._host.log(f"events_append never acknowledged a batch of {len(batch.events)}; "
                                       "replaced by a placeholder")
                        self._queue[0] = _Batch([_placeholder(batch.events[0], _UNACKED)])
                    return False
                self._queue.pop(0)
                if outcome == "refused":
                    self._queue[0:0] = self._split(batch)
        return True


class Host:
    def __init__(self, spec: launch.LaunchSpec, *, daemon=None, clock=time.monotonic, sleep=asyncio.sleep,
                 spawn=default_spawn, scan=selfcheck.scan_tree, versions=selfcheck.cli_versions,
                 scan_root: int | None = None, rand=random.random, log=_log, environ=None,
                 install=_install, install_deps=_install_deps, credential=agent_socket.credential) -> None:
        self.spec = spec
        # The engine: the launch's, or for a registry run what `_install_engine` built.
        self.argv = tuple(spec.argv)
        self.engine_env = dict(spec.env)
        self.install = install
        self.install_deps = install_deps
        self.credential = credential
        self.environ = os.environ if environ is None else environ
        self.daemon = daemon if daemon is not None else SocketDaemon()
        self.clock = clock
        self.sleep = sleep
        self.spawn = spawn
        self.scan = scan
        self.versions = versions
        self.scan_root = os.getpid() if scan_root is None else scan_root
        self.rand = rand
        self.log = log
        self.norm = events.Normaliser(session_id=spec.session_id, task_id=spec.task_id, provider=spec.provider,
                                      native_session_ref=str(spec.session_meta.get(NATIVE_REF_KEY) or ""),
                                      pricing_tier=_pricing_tier(spec.provider),
                                      input_includes_cached=_input_includes_cached(spec.provider),
                                      dialect=agent_providers.dialect_for(spec.provider))
        self.batcher = EventBatcher(self)
        self.proc = None
        self.client: acp_client.AcpClient | None = None
        self.native_ref = ""
        self._tasks: set[asyncio.Task] = set()
        # (prompt, source): who wrote it rides the turn_start event (`events.PROMPT_SOURCES`).
        self._pending: collections.deque[tuple[str, str]] = collections.deque()
        self._wake = asyncio.Event()
        self._close_requested = False
        self._handoff = False  # the close intent hands the native session to another front (`END_HANDOFF`)
        # The launch reopens an existing conversation (a resume ref; `how` resume/load, or a refused resume): no
        # first prompt, no idle end before the operator speaks.
        self._resumed = False
        self._turn_in_flight = False
        self._open_inputs = 0
        # The task is parked on a question the agent raised through the control plane itself (`input.request` from
        # its shell or an MCP tool), not an ACP elicitation: `task.get` said `awaiting_response` after the turn.
        self._awaiting_answer = False
        self._key_findings: set[tuple[int, str]] = set()
        self._last_error: tuple[str, str, str | None] | None = None  # (class, message, resets_at)
        # The break cause the engine named itself (`events.error_cause`, read through its dialect), else "".
        self._last_cause = ""
        self._break_posted: tuple[str, str, str] | None = None  # the replace-requesting break, posted once
        self._end_now: str | None = None
        self._closing = asyncio.Event()
        self._elicit_cancels: dict[str, asyncio.Future] = {}
        self._loading = False
        self._charter_in_session = False
        self.dialect = agent_providers.dialect_for(spec.provider)
        # This attempt's own dir (an SDK run's `runtime.run_dir`), "" when it has none: the run files below are
        # per attempt, and a launch's prompt dir may be shared by every slot of a pool.
        self.run_dir = str(spec.runtime.get("run_dir") or "")
        # Where the run files live: the run dir, else a private dir `run` makes when the dialect needs one (and
        # removes at the end); "" when neither.
        self.files_dir = self.run_dir
        self._files_dir_owned = False
        # The row's lifecycle events, run in front of the engine (`acp/lifecycle.py`); built by `run`.
        self.lifecycle = lifecycle.HostLifecycle()
        # Session-start context with no system prompt to ride: it heads the first prompt instead.
        self._guidance: list[str] = []
        self._last_used: int | None = None  # the last `usage_update.used`, for `events.compaction_observed`
        self._continuation = False  # the next turn is a `Stop` hook's refusal sent back (`stop_hook_active`)
        self._stop_continuations = 0
        self._session_end_run = False
        self._precompact: asyncio.Task | None = None  # the last compaction's `PreCompact`, awaited before SessionEnd
        self._fault = fault_armed(self.environ)
        self.close_grace_s = CLOSE_GRACE_S
        # How the attempt ended (a turn-loop outcome), read by `_record_break` to classify an engine that never started.
        self._outcome = ""
        self._stderr_relay = None  # `ENGINE_STDERR_ENV`'s `(fd, path)`, None when none could be made or it is done
        # A prompt the close grace gave up on (`_turn`): still read after the session closes, so the usage its late
        # result reports is not lost (`_harvest_late_turn`).
        self._late_prompt: asyncio.Future | None = None

    # ── plumbing ───────────────────────────────────────────────────────────────────────────────────

    def _spawn_task(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def call(self, verb: str, args: dict, timeout: float) -> object:
        """`daemon.call` off the loop (it blocks on a socket)."""
        return await asyncio.to_thread(self.daemon.call, verb, args, timeout=timeout)

    def _sync_call(self, verb: str, args: dict | None = None, *, timeout: float | None = None) -> object:
        """`attempt_end`'s `call` shape over the injected daemon."""
        return self.daemon.call(verb, args or {}, timeout=CALL_BUDGET_S if timeout is None else timeout)

    async def backoff(self, attempt: int) -> None:
        """Jittered exponential backoff on the injected sleep."""
        await self.sleep(min(_BACKOFF_MAX_S, 0.5 * (2 ** attempt)) * (0.5 + self.rand()))

    def emit(self, evs: list[dict]) -> None:
        for ev in evs:
            if ev.get("kind") == "error":
                data = ev.get("data") or {}
                self._last_error = (str(data.get("class") or ""), str(data.get("message") or ""),
                                    data.get("resets_at") or None)
                self._last_cause = str(data.get("cause") or "")
        self.batcher.add(evs)

    def _break_detail(self, fallback: str = "") -> str:
        """The last error's (already redacted) message, plus the reset time when it named one — spelled
        `resets Oct 18, 3:05pm (UTC)`, the form `apps/api/app/account_fold.py::_RESET_CLOCK` reads, so the
        account's usage-limit verdict lasts until the real reset rather than the default TTL."""
        _, message, resets_at = self._last_error or ("", "", None)
        detail = message or fallback
        clock = _reset_clock(resets_at) if resets_at else ""
        return f"{detail} ({clock})" if clock else detail

    def _poke(self) -> None:
        self._wake.set()

    # ── entry ──────────────────────────────────────────────────────────────────────────────────────

    async def serve(self) -> int:
        """`run`, with SIGTERM/SIGHUP shipping what was already taken before unwinding: the events batched but not
        yet sent (a turn's usage among them) flush for at most SIGNAL_FLUSH_S, then the run is cancelled — its own
        cleanup stops the engine — and the exit is `_exit_on_signal`'s."""
        loop = asyncio.get_running_loop()
        runner = asyncio.ensure_future(self.run())
        signalled: list[int] = []

        def on_signal(signum: int) -> None:
            if not signalled:
                signalled.append(signum)
                self._spawn_task(self._flush_then_cancel(runner))

        installed = {}
        for signum in (getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)):
            try:
                previous = signal.getsignal(signum)
                loop.add_signal_handler(signum, on_signal, signum)
                installed[signum] = previous
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # not the main thread, or no loop signal support: `_exit_on_signal` stays the handler
        try:
            return await runner
        except asyncio.CancelledError:
            if signalled:
                raise SystemExit(128 + signalled[0]) from None
            raise
        finally:
            for signum, previous in installed.items():
                loop.remove_signal_handler(signum)
                if previous is not None:
                    signal.signal(signum, previous)  # `main`'s `_exit_on_signal` again, for the unwinding

    async def _flush_then_cancel(self, runner: asyncio.Future) -> None:
        self.log("signalled: shipping the events already taken, then stopping")
        self._harvest_late_turn()
        try:
            await asyncio.wait_for(self.batcher.flush(attempts=2), SIGNAL_FLUSH_S)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 — best effort: the exit goes on regardless
            self.log(f"events not flushed before the exit: {type(exc).__name__}")
        runner.cancel()

    def _harvest_late_turn(self) -> None:
        """A turn the close grace gave up on whose prompt has since answered: its result (the turn's usage, its
        `turn_end`) is emitted after all, so tokens the engine reports on a cancelled turn still count."""
        prompt, self._late_prompt = self._late_prompt, None
        if prompt is None or not prompt.done() or prompt.cancelled() or prompt.exception() is not None:
            return
        result = prompt.result()
        if isinstance(result, dict):
            self.emit(self.norm.on_prompt_result(result))

    async def run(self) -> int:
        try:
            return await self._run()
        finally:
            if self._files_dir_owned:
                shutil.rmtree(self.files_dir, ignore_errors=True)

    async def _run(self) -> int:
        if not await self._install_engine() or not await self._install_dependencies():
            return 0
        self._make_files_dir()
        env = {**self.environ, **self.engine_env, ACP_HOST_ENV: "1", **self._run_files_env()}
        try:
            launch.validate_secrets(self.engine_env, self.spec.mcp_servers, self.spec.session_meta)
            # What the engine really inherits: a model key in the host's own env would ride along too.
            launch.validate_env(env, where="engine env (host environment + launch env)", secret_names=False)
        except launch.LaunchRefused as exc:
            print(f"jaah-acp-host: {exc}", file=sys.stderr, flush=True)
            if self.spec.runtime.get("run_dir"):
                # An SDK run's attempt ends named rather than as a dead pane (the message names no value).
                await self._fail_attempt(f"{LAUNCH_REFUSED} {exc}")
            return 2
        if self.spec.runtime.get("key_account") or self.spec.authenticate:
            # The host will hold a key: non-dumpable BEFORE the engine exists, so no same-uid process it starts
            # can open this process's /proc memory (an fd opened earlier would outlive a later switch).
            try:
                _make_undumpable()
            except OSError as exc:
                print(f"jaah-acp-host: could not turn non-dumpable ({exc}); refusing to start the engine",
                      file=sys.stderr, flush=True)
                return 2
        claim = getattr(self.daemon, "claim", None)
        if claim is not None:
            try:
                await asyncio.to_thread(claim, self.spec.session_id)
            except (agent_socket.AgentSocketError, OSError) as exc:
                # Fail CLOSED in a MicroVM guest: an unclaimed capability would be the agent's to claim. A static
                # node's host needs none.
                if os.path.isdir(GUEST_LAUNCH_DIR):
                    print(f"jaah-acp-host: runtime.host_claim failed ({exc}); refusing to start the engine",
                          file=sys.stderr, flush=True)
                    return 2
                self.log(f"runtime.host_claim failed: {exc}")
        self._stderr_relay = _engine_stderr_file(self.log)
        if self._stderr_relay is not None:
            env[ENGINE_STDERR_ENV] = self._stderr_relay[1]
        hooks = self.spec.runtime.get("lifecycle_hooks")
        self.lifecycle = lifecycle.HostLifecycle(self.dialect.host_lifecycle, hooks if isinstance(hooks, dict) else None,
                                                 env=env, cwd=self.spec.worktree, log=self.log)
        if hooks and not (self.lifecycle.events and self.files_dir):
            # FAIL CLOSED: the launch took these rows (the quick gate, the guard brief) out of the engine's own hooks,
            # so a host that cannot run them must not start it (a dialect that declares none, or no files dir).
            print("jaah-acp-host: the launch's lifecycle hooks cannot run here (no host lifecycle or no run files "
                  "dir); refusing to start the engine", file=sys.stderr, flush=True)
            return 2
        try:
            self.proc = await self.spawn(list(self.argv), cwd=self.spec.worktree, env=env)
        except (OSError, ValueError) as exc:
            # No engine: still a classified attempt end (a break on the task), never a bare traceback.
            self.log(f"engine {self.argv[0]!r} did not start: {type(exc).__name__}: {exc}")
            self.emit(self.norm.error("agent", f"the engine {self.argv[0]!r} did not start: "
                                                f"{type(exc).__name__}: {exc}"))
            await self._end(END_SPAWN_FAILED, None)
            return 0
        conn = jsonrpc.Connection(self.proc.stdout, self.proc.stdin, on_request=self._on_request,
                                  on_notification=self._on_notification)
        self.client = acp_client.AcpClient(conn)
        reader = self._spawn_task(conn.run())
        exited = self._spawn_task(self.proc.wait())
        turns = self._spawn_task(self._turn_loop())
        control = None
        try:
            control = self._spawn_task(self._control_lane())
            await asyncio.wait({turns, exited, reader}, return_when=asyncio.FIRST_COMPLETED)
            if reader.done() and not exited.done() and (not turns.done() or not self.native_ref):
                # stdout closed: usually the engine dying — let its exit name the outcome if it is one. That
                # includes a turn loop the close already ended before any session (initialize saw it first).
                await asyncio.wait({exited}, timeout=READER_EXIT_GRACE_S)
                if not exited.done() and _has_exited(self.proc.pid):  # type: ignore[attr-defined]  # spawned above
                    # Dead but not yet reaped: a stalled loop runs the grace timer before the exit it waits for.
                    await asyncio.wait({exited}, timeout=STOP_GRACE_S)
            if exited.done():
                turns.cancel()
                outcome = "engine_exit"
                exit_code = exited.result()
            elif turns.done():
                outcome = self._turn_outcome(turns)
                await self._close_session()
                exit_code = await self._stop_engine(exited)
                self._harvest_late_turn()
            else:
                turns.cancel()
                outcome = END_CONNECTION_CLOSED
                self.log("engine stdout closed with the engine still running; stopping it")
                exit_code = await self._stop_engine(exited)
            await self._end(outcome, exit_code)
            return 0
        finally:
            for task in (control, reader, turns):
                if task is not None and not task.done():
                    task.cancel()
            # Unwinding on a signal (`_exit_on_signal`): the engine never outlives the host, so `main` never
            # removes a run dir under a live engine.
            if self.proc is not None and self.proc.returncode is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass

    async def _install_engine(self) -> bool:
        """A registry run (`runtime.install`): install the pin and take its argv and env as the engine. False when
        it failed — reported as the attempt's `task.fail`, so the run ends named rather than as a dead pane."""
        install = self.spec.runtime.get("install")
        if not install:
            return True
        self.log(f"installing {install['slug']} into {install['dest']}")
        try:
            argv, env = await asyncio.to_thread(self.install, install)
            launch.validate_env(env, where=f"{install['slug']}'s registry env")
        except launch.LaunchRefused as exc:
            await self._fail_attempt(f"{INSTALL_FAILED} {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 — `InstallFailed` names the agent and cause; anything else is named too
            await self._fail_attempt(f"{INSTALL_FAILED} {getattr(exc, 'sentence', '') or f'{type(exc).__name__}: {exc}'}")
            return False
        self.argv = tuple(argv)
        self.engine_env = {**self.engine_env, **env}
        return True

    async def _install_dependencies(self) -> bool:
        """An SDK bundle run (`runtime.deps`): install what the bundle declares beside its script before the engine
        starts; the argv is unchanged. False when it failed — reported as the attempt's `task.fail`, named."""
        deps = self.spec.runtime.get("deps")
        if not deps:
            return True
        self.log(f"installing {deps['name']}'s dependencies into {deps['dest']}")
        try:
            await asyncio.to_thread(self.install_deps, deps)
        except Exception as exc:  # noqa: BLE001 — `InstallFailed` names the agent and cause; anything else is named too
            await self._fail_attempt(f"{INSTALL_FAILED} {getattr(exc, 'sentence', '') or f'{type(exc).__name__}: {exc}'}")
            return False
        return True

    async def _fail_attempt(self, reason: str) -> None:
        """Report `reason` as this attempt's `task.fail`, once the launcher's claim shows (a fail sent while the
        task is still `ready` is refused and the reason lost) — the staging-failure lines' order."""
        self.log(reason)
        deadline = self.clock() + CLAIM_WAIT_S
        while self.clock() < deadline:
            try:
                data = await self.call("task.get", {}, CALL_BUDGET_S)
                if isinstance(data, dict) and data.get("state") in _CLAIMED_STATES:
                    break
            except agent_socket.AgentSocketError:
                pass
            await self.sleep(0.5)
        try:
            await self.call("task.fail", {"reason": reason, "session_id": self.spec.session_id}, CALL_BUDGET_S)
        except agent_socket.AgentSocketError as exc:
            self.log(f"task.fail not recorded: {exc}")

    async def _end(self, outcome: str, exit_code: int | None) -> None:
        """`session_end`, every event flushed, then — unless the task closed itself or a hand-off moved it to the
        next front — `end_attempt`."""
        self._outcome = outcome
        self.log(f"attempt ended outcome={outcome} exit_code={exit_code}")
        await self._drain_stderr()
        said = _release_relay(self._stderr_relay, read=not self.native_ref)
        self._stderr_relay = None
        if said and not self.native_ref:
            # Never started: what the engine's wrapper said on its way out is the cause, not the engine's echo of it.
            self.emit(self.norm.error("agent", f"the engine exited before its session opened: {said}"))
        await self._session_end(outcome)
        self.emit(self.norm.session_end(exit_code, outcome))
        await self.batcher.flush()
        # A hand-off leaves the task `active`: the next front's session is the attempt, not a break.
        if outcome not in (END_TASK_CLOSED, END_HANDOFF):
            await asyncio.to_thread(attempt_end.end_attempt, record_break=self._record_break,
                                    log=self.log, call=self._sync_call)

    async def _session_end(self, outcome: str) -> None:
        """`SessionEnd`, once, for a session that opened: an operator's close is the hook contract's
        `prompt_input_exit` (the quit at the prompt), any other end `other`."""
        if self._session_end_run or not self.native_ref:
            return
        self._session_end_run = True
        if self._precompact is not None and not self._precompact.done():
            await asyncio.wait({self._precompact}, timeout=lifecycle.END_BUDGET_S)
        reason = "prompt_input_exit" if outcome == END_CLOSE_INTENT else "other"
        await self.lifecycle.fire("SessionEnd", self._hook_session(), budget=lifecycle.END_BUDGET_S, reason=reason)

    async def _drain_stderr(self) -> None:
        """Let the engine's stderr relay show what is still in the pipe (`default_spawn`), up to a bound: the last
        lines an exiting engine writes are usually what names its exit."""
        relay = getattr(self.proc, "jaah_stderr_relay", None)
        if relay is None or relay.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(relay), STDERR_DRAIN_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    def _turn_outcome(self, turns: asyncio.Task) -> str:
        try:
            return turns.result()
        except Exception as exc:  # noqa: BLE001 — a host bug is an outcome: the attempt still ends cleanly
            self.log(f"turn loop failed: {type(exc).__name__}: {exc}")
            return END_HOST_ERROR

    async def _close_session(self) -> None:
        if self.client is None or not self.native_ref or self.client.conn.closed:
            return
        try:
            await asyncio.wait_for(self.client.close(self.native_ref), STOP_GRACE_S)
        except (jsonrpc.JsonRpcError, jsonrpc.ConnectionClosed, asyncio.TimeoutError) as exc:
            self.log(f"session/close failed: {type(exc).__name__}")

    async def _stop_engine(self, exited: asyncio.Task) -> int | None:
        if not exited.done():
            if os.name == "nt" and self.proc is not None:
                # TerminateProcess ends the `.cmd` shim's cmd.exe only, not the node.exe it started.
                await asyncio.to_thread(_kill_tree, self.proc.pid)
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            done, _ = await asyncio.wait({exited}, timeout=STOP_GRACE_S)
            if not done:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                await exited
        return exited.result()

    def _record_break(self) -> tuple[str, str, str]:
        """`end_attempt`'s break recorder: the cause is the last error class seen, else `unknown`. A
        replace-requesting break already posted this attempt is the record — never a second one."""
        if self._break_posted is not None:
            self.log(f"settle runner break already recorded cause={self._break_posted[0]}")
            return self._break_posted
        cls = (self._last_error or ("", "", None))[0]
        # No native session: the engine died (or was stopped) before `session/new` answered, whatever the outcome.
        never_started = self._outcome in (END_SPAWN_FAILED, END_INIT_FAILED) or not self.native_ref
        cause = self._last_cause or _BREAK_CAUSE.get(cls) or (_START_CAUSE if never_started else "unknown")
        detail = self._break_detail("the ACP engine's attempt ended with the task still open and no evidence")
        if never_started:
            detail = f"the ACP engine never started a session ({self._outcome}): {detail}"
        payload = {"cause": cause, "detail": detail, "session_id": self.spec.session_id,
                   "observed_by": _OBSERVED_BY}
        try:
            self._sync_call("session.error", payload)
            self.log(f"settle recorded runner break cause={cause}")
        except agent_socket.AgentSocketRefused as exc:
            self.log(f"settle runner-break record rejected reason={exc.reason} (non-fatal)")
        except agent_socket.AgentSocketUnavailable:
            self.log("settle runner-break record best-effort failed (non-fatal)")
        return cause, detail, self.spec.session_id

    # ── turns and supervision ──────────────────────────────────────────────────────────────────────

    def _read_prompt_file(self, path: str) -> str:
        if not path:
            return ""
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.log(f"prompt file unreadable {path}: {type(exc).__name__}")
            return ""

    def _make_files_dir(self) -> None:
        """A private files dir for a run with no run dir whose dialect writes run files (host lifecycle, the context
        spool, a system prompt file); removed when `run` unwinds. A failure leaves none, and the run goes on without."""
        dialect = self.dialect
        if self.files_dir or not (dialect.host_lifecycle or dialect.context_channel == "turn_boundary"
                                  or dialect.system_prompt_file_env):
            return
        try:
            self.files_dir = tempfile.mkdtemp(prefix="jaah-acp-")
            self._files_dir_owned = True
        except OSError as exc:
            self.log(f"run files dir not made: {type(exc).__name__}; no host lifecycle or hook context this run")

    def _run_files_env(self) -> dict[str, str]:
        """The run files the engine's env names: the hook context spool, and the system prompt file when the dialect
        declares one (`AcpDialect.system_prompt_file_env`). None without a files dir."""
        if not self.files_dir:
            return {}
        env = {HOOK_CONTEXT_ENV: os.path.join(self.files_dir, HOOK_CONTEXT_FILE)}
        if self.dialect.system_prompt_file_env:
            env[self.dialect.system_prompt_file_env] = os.path.join(self.files_dir, SYSTEM_PROMPT_FILE)
        return env

    def _write_system_prompt_file(self, text: str) -> bool:
        """Write `text` (the charter and the session-start context) to `<files dir>/system-prompt.md` (0600) for an
        engine whose dialect names `system_prompt_file_env`, before `session/new`; True when the session carries it
        that way. A no-op for every other engine, and when there is no files dir or no text."""
        if not (self.dialect.system_prompt_file_env and self.files_dir and text):
            return False
        path = os.path.join(self.files_dir, SYSTEM_PROMPT_FILE)
        try:
            if os.path.lexists(path):
                os.unlink(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            self.log(f"system prompt file not written {path}: {type(exc).__name__}; the charter heads the prompt")
            return False
        return True

    def _hook_session(self) -> str:
        """The session id a host-run hook is told: the engine's own (what its in-band hooks see), else the run's."""
        return self.native_ref or self.spec.session_id

    async def _session_start_context(self) -> str:
        """`SessionStart`, run once before the session opens (`acp/lifecycle.py`): its context — the guard brief, the
        context router's routing notes — for the system prompt."""
        if not self.lifecycle.runs("SessionStart"):
            return ""
        # The engine's own session id is known only on a resume; a new session is named by the run's until it opens.
        resume = str(self.spec.session_meta.get(NATIVE_REF_KEY) or "")
        verdict = await self.lifecycle.fire("SessionStart", resume or self.spec.session_id,
                                            source="resume" if resume else "startup")
        return verdict["context"]

    def _drain_hook_context(self, *extra: str) -> str:
        """The hook context owed to the model at this prompt: session-start context no system prompt took, the
        context spool (`$JAAH_HOOK_CONTEXT`, taken whole), then `extra` (this prompt's `UserPromptSubmit`)."""
        parts, self._guidance = list(self._guidance), []
        if self.files_dir and self.dialect.context_channel == "turn_boundary":
            parts.append(lifecycle.drain_spool(os.path.join(self.files_dir, HOOK_CONTEXT_FILE)))
        parts.extend(extra)
        return lifecycle.capped("\n\n".join(part.strip() for part in parts if part and part.strip()))

    def _first_prompt(self) -> str:
        """The task prompt, headed by the charter unless the session already carries it as its system prompt."""
        paths = (self.spec.prompt_path,) if self._charter_in_session else (self.spec.charter_path,
                                                                             self.spec.prompt_path)
        return "\n\n".join(text for text in map(self._read_prompt_file, paths) if text)

    async def _start_session(self) -> bool:
        import agent_vocabulary

        # The run identity, and the `jaah.ai` extension methods `_forward` serves (an SDK v2 agent calls none else).
        meta = {"task_id": self.spec.task_id, "session_id": self.spec.session_id,
                "provider": self.spec.provider, "mode": modes.LAUNCH_MODE}
        if self.spec.runtime.get("profile") != PROFILE_ACP_CORE:
            meta.update(agent_vocabulary.extension_capability())
        # `native_session_ref` is ours (the resume target); every other key is the provider's own `_meta`.
        agent_meta = {k: v for k, v in self.spec.session_meta.items() if k != NATIVE_REF_KEY}
        try:
            await self.client.initialize(jaah_meta=meta)
            if not await self._authenticate():
                return False
            # An agent that takes a system prompt gets the charter there, as the CLI's `--append-system-prompt`: ACP
            # `systemPrompt` when advertised, else the `_meta` path its dialect names (`AcpDialect.system_prompt_meta`),
            # else the file it names (`system_prompt_file_env`). The session-start hooks' context (the guard brief
            # among it) joins it, as a native hook layer adds a SessionStart hook's output to the starting context.
            start_context = await self._session_start_context()
            native = self.client.takes_system_prompt()
            meta_path = self.dialect.system_prompt_meta
            charter = self._read_prompt_file(self.spec.charter_path)
            system = "\n\n".join(text for text in (charter, start_context) if text)
            if system and meta_path and not native:
                agent_meta = _with_meta_path(agent_meta, meta_path, system)
            in_file = not (native or meta_path) and self._write_system_prompt_file(system)
            placed = bool(native or meta_path or in_file)
            if start_context and not placed:
                self._guidance.append(start_context)
            # `session/load` replays the old conversation as updates: history, not new events.
            self._loading = True
            try:
                self.native_ref, how = await self.client.open_session(
                    cwd=self.spec.worktree, mcp_servers=[dict(s) for s in self.spec.mcp_servers],
                    additional_dirs=list(self.spec.additional_dirs), meta=meta, agent_meta=agent_meta,
                    system_prompt=system if native else "",
                    resume_ref=str(self.spec.session_meta.get(NATIVE_REF_KEY) or ""))
            finally:
                self._loading = False
            self._charter_in_session = bool(charter) and placed
            # A launch naming a session to reopen (a console hand-off) never re-sends the task, even when the engine
            # fell back to `session/new`: that would start the task over beside the conversation it was handed.
            self._resumed = how in ("resume", "load") or bool(self.spec.session_meta.get(NATIVE_REF_KEY))
            if self._resumed and how == "new":
                self.log("resume refused by the engine; a new conversation waits for the operator")
        except (jsonrpc.JsonRpcError, jsonrpc.ConnectionClosed) as exc:
            self.emit(self.norm.on_prompt_error(exc)[:-1])  # the error, without a turn_end for no turn
            if self._last_cause:
                # An engine-named cause (a refused account) is the same replace-break a failed prompt posts.
                await self._on_prompt_failed()
            return False
        self.norm.native_session_ref = self.native_ref
        # A session opened: the relay's job (naming a start failure) is done, so its file goes now.
        _release_relay(self._stderr_relay, read=False)
        self._stderr_relay = None
        findings = self._new_findings()
        # `answer_receipts`: this host reports every answer it takes (`answer_received`), so the console may read a
        # missing receipt as "never reached the node"; an older host's stream says nothing either way.
        # `agent`: who the engine says it is (`initialize`'s `agentInfo`), the session's label; `models`: what its
        # session offered (`acp_client.model_choice`), sensed where the agent advertises them.
        client = self.client
        assert client is not None  # opened above
        start = {
            "key_in_env": findings, "cli_versions": self.versions(_engine_argv0(self.argv)),
            "mode": modes.LAUNCH_MODE, "how": how, "capabilities": client.agent_capabilities,
            "answer_receipts": True,
        }
        if client.agent_info:
            start["agent"] = dict(client.agent_info)
        if client.models:
            start["models"] = dict(client.models)
        self.emit(self.norm.session_start(start))
        self._report_findings(findings)
        return True

    async def _authenticate(self) -> bool:
        """Sign in with the run's account key before the session. A launch naming `authenticate` (a model runner's
        API-key account) names the method and `_meta` key; its key is the daemon's
        `runtime.account_key` answer, served once, for the runner's leased account. Otherwise a bound account
        (`runtime.key_account` under key route `acp_authenticate`, an SDK agent's) signs in with the first API-key
        method of the agent's `authMethods`, its key read from the broker. Either way it is sent in `_meta`
        (`acp_client.AcpClient.authenticate`); False, with a named `agent` error, when it fails — and no session is
        opened. The key is held in this frame only, and engine text that carries any of it is never quoted."""
        auth = self.spec.authenticate
        if auth:
            slug, method, key_meta = auth["account"], auth["method_id"], auth["key_meta"]
            try:
                answer = await self.call("runtime.account_key", {"session_id": self.spec.session_id}, KEY_FETCH_S)
            except agent_socket.AgentSocketError as exc:
                return self._auth_failed(f"the key of account {slug} could not be read ({exc})")
            key = answer.get("key") if isinstance(answer, dict) else None
            if not isinstance(key, str) or not key:
                return self._auth_failed(f"the daemon answered no key for account {slug}")
            return await self._sign_in(slug, method, key, key_meta)
        slug = str(self.spec.runtime.get("key_account") or "")
        if not slug or self.spec.runtime.get("key_delivery") != KEY_DELIVERY_AUTHENTICATE:
            return True
        method = acp_client.api_key_method(self.client.auth_methods)
        if not method:
            offered = ", ".join(str(m.get("id")) for m in self.client.auth_methods if m.get("id")) or "none"
            return self._auth_failed(f"the agent offers no API-key sign-in (its auth methods: {offered})")
        try:
            key = await asyncio.to_thread(self.credential, ACCOUNT_KEY_PREFIX + slug, timeout=KEY_FETCH_S)
        except agent_socket.AgentSocketError as exc:
            return self._auth_failed(f"the key of account {slug} could not be read ({exc})")
        return await self._sign_in(slug, method, key, acp_client.API_KEY_META)

    async def _sign_in(self, slug: str, method: str, key: str, key_meta: str) -> bool:
        try:
            await self.client.authenticate(method, key, key_meta=key_meta)
        except jsonrpc.JsonRpcError as exc:
            said = str(events.error_detail(exc.data) or exc.message)[:_ENGINE_TEXT_MAX]
            if _shares_key(said, key):
                # Any 8-character run of the key (an echo, a truncation) drops the engine's text altogether.
                said = f"error {exc.code}"
            return self._auth_failed(f"the agent refused sign-in with account {slug} ({method}): {said}")
        self.log(f"signed in with {method} (account {slug})")
        return True

    def _auth_failed(self, sentence: str) -> bool:
        self.log(f"{AUTH_FAILED} {sentence}")
        # `agent`, not `auth`: `auth` reads as an expired OAuth login (cause `auth_expired`), whose remedy is a re-login.
        self.emit(self.norm.error("agent", f"{AUTH_FAILED} {sentence}"))
        return False

    def _new_findings(self) -> list[dict]:
        fresh = []
        for finding in self.scan(self.scan_root):
            key = (finding.get("pid"), finding.get("var"))
            if key not in self._key_findings:
                self._key_findings.add(key)
                fresh.append(finding)
        return fresh

    def _report_findings(self, findings: list[dict]) -> None:
        # Straight to the batcher: a self-check finding is not the engine's error and never a break cause.
        for finding in findings:
            self.batcher.add(self.norm.error("auth",
                                             f"model key found in environment of {finding.get('comm') or '?'}"))

    async def _turn(self, text: str, source: str = "task") -> str | None:
        """One prompt. Returns the stop reason ("error" on a failed prompt, "refused" when a `UserPromptSubmit`
        hook refused it unsent). A dead connection parks here: the engine's exit is what ends the attempt, and the
        exit watcher wins the race. Around it the row's host lifecycle runs (`acp/lifecycle.py`): `UserPromptSubmit`
        before, its context and the spool's at the head of the prompt (`_drain_hook_context`), `Stop` after."""
        continuation, self._continuation = self._continuation, False
        submit = await self.lifecycle.fire("UserPromptSubmit", self._hook_session(), prompt=text)
        if submit["decision"] == "deny":
            self.log(f"a UserPromptSubmit hook refused the {source} prompt: {submit['reason'][:200]}")
            return "refused"
        if self._close_requested:
            # A close that came while the hook ran: this prompt is never sent.
            return "cancelled"
        self.emit(self.norm.turn_start(source, text))
        self._turn_in_flight = True
        guidance = ""
        if self._fault:
            # The dev check: one recorded API-error frame instead of this prompt, then the run goes on as usual.
            self._fault = False
            self.log(f"{FAULT_ENV}={FAULT_API_ERROR}: failing this prompt with the recorded API-error frame")
            prompt = asyncio.ensure_future(_fail_with(jsonrpc.JsonRpcError(*FAULT_FRAME)))
        else:
            guidance = self._drain_hook_context(submit["context"])
            heading = {"guidance": f"{GUIDANCE_HEADING}\n\n{guidance}"} if guidance else {}
            prompt = asyncio.ensure_future(self.client.prompt(self.native_ref, text, **heading))
        # Read on every path, so a prompt abandoned by a cancelled turn never logs "never retrieved".
        prompt.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            closing = asyncio.ensure_future(self._closing.wait())
            try:
                await asyncio.wait({prompt, closing}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                closing.cancel()
            if not prompt.done():
                # Closing: the turn was cancelled; give the engine a bounded while to wind it down.
                done, _ = await asyncio.wait({prompt}, timeout=self.close_grace_s)
                if not done:
                    self.log(f"close: prompt still running after {self.close_grace_s:.0f}s; stopping the engine")
                    self._late_prompt = prompt  # read again once the session is closed (`_harvest_late_turn`)
                    return None
            try:
                result = prompt.result()
            except jsonrpc.ConnectionClosed as exc:
                self.emit(self.norm.on_prompt_error(exc))
                await asyncio.Event().wait()
            except jsonrpc.JsonRpcError as exc:
                self.emit(self.norm.on_prompt_error(exc))
                stop = "error"
                if guidance:
                    self._guidance.insert(0, guidance)  # the engine refused the prompt it rode: owed again
                await self._on_prompt_failed()
            else:
                self.emit(self.norm.on_prompt_result(result))
                stop = str(result.get("stopReason") or "")
                if self.norm.text_failed:
                    # The turn ended, but its agent text was an error the dialect names a cause for
                    # (`events.Normaliser._text_failure`): the same break a failed prompt posts.
                    stop = "error"
                    await self._on_prompt_failed()
                else:
                    self._last_error = None
                    self._last_cause = ""
        finally:
            if not prompt.done() and prompt is not self._late_prompt:
                prompt.cancel()
            self._turn_in_flight = False
        self._report_findings(self._new_findings())
        if stop and stop not in ("error", "cancelled") and not self._close_requested:
            await self._turn_end(continuation)
        return stop

    async def _turn_end(self, continuation: bool) -> None:
        """`Stop`, as the hook contract runs it when the agent finishes responding: a refusal (the quick gate red, a prose ask)
        sends its reason back as the next prompt, `stop_hook_active` set on the `Stop` after it, at most
        `MAX_STOP_CONTINUATIONS` times in a row."""
        if not self.lifecycle.runs("Stop"):
            return
        self._stop_continuations = self._stop_continuations + 1 if continuation else 0
        verdict = await self.lifecycle.fire("Stop", self._hook_session(), stop_hook_active=continuation,
                                            last_assistant_message=self.norm.turn_text)
        if verdict["decision"] != "deny" or not verdict["reason"].strip() or self._close_requested:
            return
        if self._stop_continuations >= MAX_STOP_CONTINUATIONS:
            self.log(f"a Stop hook refused the turn end {self._stop_continuations + 1} times running; letting it end")
            return
        self._continuation = True
        self._pending.appendleft((verdict["reason"].strip(), "nudge"))

    async def _on_prompt_failed(self) -> None:
        """A usage-limit or auth prompt error, or one whose break cause the engine named (the ACP face of a CLI's
        stop-failure hook, `events.error_cause`): post the
        break (with `request_replace`) once, then end the attempt now; if the break never lands, end it all the
        same — `end_attempt` records it — rather than idle out the linger on a dead account or a failing API. Any other prompt error ends the
        attempt too, its break classified by `_record_break` from the error's class: a turn the engine failed
        leaves nothing to linger for (no nudge follows an `error` stop)."""
        if self._break_posted is not None:
            return
        cls = (self._last_error or ("", "", None))[0]
        # A usage limit or an auth refusal replaces on the first error (`_REPLACE_CLASSES`); an API error only when
        # the engine named its cause, so a transient error a vendor merely words like one never swaps the account.
        if not (cls in _REPLACE_CLASSES or self._last_cause):
            self.log(f"prompt failed (class={cls or 'unknown'}); ending the attempt")
            self._end_now = END_AGENT_ERROR
            return
        cause = self._last_cause or _BREAK_CAUSE[cls]
        detail = self._break_detail()
        payload = {"cause": cause, "detail": detail, "session_id": self.spec.session_id,
                   "observed_by": _OBSERVED_BY, "request_replace": True}
        for attempt in range(BREAK_RETRIES):
            try:
                await self.call("session.error", payload, CALL_BUDGET_S)
            except agent_socket.AgentSocketRefused as exc:
                if _reason(exc) not in _RETRYABLE:
                    self.log(f"runner-break {cause} refused reason={exc.reason}")
                    break
            except agent_socket.AgentSocketUnavailable:
                pass
            else:
                self._break_posted = (cause, detail, self.spec.session_id)
                self.log(f"recorded runner break cause={cause} request_replace=true")
                # Ended now, not on a later `task.get` or the idle linger: a refused key or a spent account fails
                # every later turn. `end_attempt` reads the break already posted (`_record_break`).
                self._end_now = END_BREAK_RECORDED
                return
            if attempt + 1 < BREAK_RETRIES:
                await self.backoff(attempt)
        self.log(f"runner-break {cause} not recorded; ending the attempt")
        self._end_now = END_BREAK_UNRECORDED

    async def _task_open(self) -> bool:
        try:
            data = await self.call("task.get", {}, CALL_BUDGET_S)
        except agent_socket.AgentSocketRefused as exc:
            if _reason(exc) in _RETRYABLE:
                # The daemon or the control plane could not answer now: as below, no verdict.
                self.log(f"task.get refused reason={exc.reason}; treating the task as open")
                return True
            self.log(f"task.get refused reason={exc.reason}; treating the task as closed")
            return False
        except agent_socket.AgentSocketUnavailable:
            # No answer is not a verdict: closing on a daemon restart would end a healthy run.
            self.log("task.get unavailable; treating the task as open")
            return True
        if not isinstance(data, dict) or not data:
            return False  # `task.get` resolves only an open row: nothing back means it is no longer open
        self._awaiting_answer = data.get("state") == AWAITING_STATE
        return data.get("state") not in _CLOSED_STATES

    def _has_work(self) -> bool:
        return bool(self._pending) or self._close_requested

    async def _wait(self, deadline: float | None) -> str:
        """Until work arrives ("work"), the wake fires without work ("changed"), or the injected clock passes
        `deadline` ("timeout"; never when `deadline` is None)."""
        if self._has_work():
            return "work"
        self._wake.clear()
        waiter = asyncio.ensure_future(self._wake.wait())
        try:
            if deadline is None:
                await waiter
            else:
                while not waiter.done():
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        return "work" if self._has_work() else "timeout"
                    sleeper = asyncio.ensure_future(self.sleep(remaining))
                    await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
                    sleeper.cancel()
        finally:
            waiter.cancel()
        return "work" if self._has_work() else "changed"

    async def _turn_loop(self) -> str:
        if not await self._start_session():
            return END_INIT_FAILED
        # A resumed conversation already holds the charter and the task: re-sending them would restart the work.
        first = "" if self._resumed else self._first_prompt()
        if first:
            self._pending.append((first, "task"))
        turned = False
        nudged = False
        stop: str | None = None
        linger = float(self.spec.runtime.get("idle_linger_s", DEFAULT_IDLE_LINGER_S))
        idle_deadline: float | None = None  # survives wakes that bring no work
        while True:
            if self._close_requested:
                return END_HANDOFF if self._handoff else END_CLOSE_INTENT
            if self._pending:
                idle_deadline = None
                stop = await self._turn(*self._pending.popleft())
                turned = True
                if self._end_now:
                    return self._end_now
                if self._close_requested:
                    return END_HANDOFF if self._handoff else END_CLOSE_INTENT
                if not await self._task_open():
                    return END_TASK_CLOSED
                continue
            # Idle, after a turn (or before any when there was no first prompt).
            # A resumed conversation waits for the operator with no idle end until a turn has run.
            if self._open_inputs or (self._resumed and not turned):
                idle_deadline = None
                await self._wait(None)
                continue
            if self._awaiting_answer:
                # A question waits for the operator: no linger and no nudge, however long. Re-read the task
                # meanwhile; back to `active` with no answer re-arms a fresh linger (the question turn's end owes
                # no nudge; the answer's turn still may).
                idle_deadline, stop = None, None
                if await self._wait(self.clock() + TASK_POLL_S) == "timeout" and not await self._task_open():
                    return END_TASK_CLOSED
                continue
            if stop == "end_turn" and not nudged:
                nudged = True
                self._pending.append((str(self.spec.runtime.get("nudge") or DEFAULT_NUDGE), "nudge"))
                continue
            if idle_deadline is None:
                idle_deadline = self.clock() + linger
            woke = await self._wait(min(idle_deadline, self.clock() + TASK_POLL_S))
            if woke != "timeout" or self._open_inputs:
                continue
            # The task's state may have moved mid-linger (a question raised, the task closed).
            if not await self._task_open():
                return END_TASK_CLOSED
            if not self._awaiting_answer and self.clock() >= idle_deadline:
                return END_IDLE

    # ── control lane ───────────────────────────────────────────────────────────────────────────────

    async def _control_lane(self) -> None:
        cursor = 0
        failures = 0
        while True:
            try:
                res = await self.call("runtime.control_wait",
                                      {"session_id": self.spec.session_id, "after": cursor}, WAIT_BUDGET_S)
            except agent_socket.AgentSocketRefused as exc:
                if _reason(exc) not in _RETRYABLE:
                    self.log(f"control_wait refused reason={exc.reason}")
                await self.backoff(failures)
                failures += 1
                continue
            except agent_socket.AgentSocketUnavailable:
                await self.backoff(failures)
                failures += 1
                continue
            failures = 0
            res = res if isinstance(res, dict) else {}
            for intent in res.get("intents") or ():
                if isinstance(intent, dict):
                    await self._on_intent(intent)
            nxt = res.get("cursor")
            if isinstance(nxt, int) and nxt > cursor:
                cursor = nxt

    async def _on_intent(self, intent: dict) -> None:
        kind = intent.get("type")
        payload = intent.get("payload") if isinstance(intent.get("payload"), dict) else {}
        if kind in ("steer", "answer"):
            text = str(payload.get("text") or "")
            if text:
                self._pending.append((text, "operator"))
                self._poke()
            if kind == "answer":
                # The receipt: a closing session never runs its queue, and a blank answer is nothing.
                decision = "dropped" if not text or self._close_requested else "queued"
                self.emit(self.norm.answer_received(decision, via="prompt", control_id=str(intent.get("id") or "")))
        elif kind == "interrupt":
            self._cancel_elicitations()
            if self._turn_in_flight:
                await self._cancel_turn()
        elif kind == "close":
            self._handoff = self._handoff or payload.get("handoff") is True
            self._close_requested = True
            self._closing.set()
            self._cancel_elicitations()
            if self._turn_in_flight:
                await self._cancel_turn()
            self._poke()
        else:
            self.log(f"unknown control intent type={kind!r} ignored")

    def _cancel_elicitations(self) -> None:
        for fut in self._elicit_cancels.values():
            if not fut.done():
                fut.set_result(None)

    async def _cancel_turn(self) -> None:
        try:
            await self.client.cancel(self.native_ref)
        except jsonrpc.ConnectionClosed:
            pass

    # ── engine -> client ───────────────────────────────────────────────────────────────────────────

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "session/update" and not self._loading:
            update = params.get("update")
            if isinstance(update, dict):
                self.emit(self.norm.on_update(update))
                self._watch_compaction(update)

    def _watch_compaction(self, update: dict) -> None:
        """`PreCompact` (the read-once guard's reset) when the `usage_update` sequence shows a compaction
        (`events.compaction_observed`): the engine reports none of its own."""
        used = events.usage_used(update)
        if used is None:
            return
        if (events.compaction_observed(self._last_used, used) and self.lifecycle.runs("PreCompact")
                and (self._precompact is None or self._precompact.done())):
            self.log(f"compaction observed: context {self._last_used} -> {used} tokens")
            self._precompact = self._spawn_task(self.lifecycle.fire("PreCompact", self._hook_session(),
                                                                    trigger="auto", custom_instructions=""))
        self._last_used = used

    async def _on_request(self, method: str, params: dict, request_id) -> object:
        if method == "session/request_permission":
            # A question the agent asks through a permission request (`requests.is_agent_question`) goes to the
            # operator; every other permission is answered by policy.
            if requests.is_agent_question(
                    params, agent_providers.dialect_for(self.spec.provider).question_call_prefix):
                return await self._ask_question(params)
            option = requests.choose_permission(params.get("options") or [])
            self.emit(self.norm.approval_request(requests.approval_data(params, option)))
            return requests.permission_response(option)
        if method == "elicitation/create":
            return await self._elicit(params)
        if method.startswith(_VERB_PREFIX):
            return await self._forward(method[len(_VERB_PREFIX):].replace("/", "."), params)
        raise jsonrpc.JsonRpcError(jsonrpc.METHOD_NOT_FOUND, f"Method not found: {method}")

    async def _forward(self, verb: str, params: dict) -> object:
        """A `_jaah.ai/<verb>` request: TASK verbs only, refusals as JSON-RPC errors."""
        import agent_vocabulary

        known = agent_vocabulary.BY_NAME.get(verb)
        if known is None or known.scope is not agent_vocabulary.Scope.TASK:
            raise jsonrpc.JsonRpcError(jsonrpc.METHOD_NOT_FOUND, f"Method not found: {_VERB_PREFIX}{verb}")
        try:
            # The canonical name: a pre-rename method (`_jaah.ai/task/settle`) reaches the same row.
            return await self.call(known.name, params, known.timeout_s)
        except agent_socket.AgentSocketRefused as exc:
            raise jsonrpc.JsonRpcError(-32000, f"refused: {exc.reason}") from None
        except agent_socket.AgentSocketUnavailable:
            raise jsonrpc.JsonRpcError(-32000, "unavailable") from None

    async def _retrying(self, verb: str, args: dict, timeout: float) -> object:
        """`verb` until it answers; retry on busy/unavailable with jittered backoff, raise other refusals."""
        attempt = 0
        while True:
            try:
                return await self.call(verb, args, timeout)
            except agent_socket.AgentSocketRefused as exc:
                if _reason(exc) not in _RETRYABLE:
                    raise
            except agent_socket.AgentSocketUnavailable:
                pass
            await self.backoff(attempt)
            attempt += 1

    async def _elicit(self, params: dict) -> dict:
        """An `elicitation/create`: the operator's answers mapped back onto its schema (`_ask_human`). Answered per
        the elicitation spec, never with a JSON-RPC error: a mode this client did not advertise (only `form` is,
        `acp_client.CLIENT_CAPABILITIES`; a `url` one would send the operator off-node) or a form the lane cannot
        represent is declined, so the agent goes on without it."""
        mode = params.get("mode")
        if mode not in (None, "form"):
            self.log(f"elicitation mode {mode!r} not supported; declined")
            return {"action": "decline"}
        try:
            dialect = agent_providers.dialect_for(self.spec.provider)
            req = requests.request_from_elicitation(
                params, uuid.uuid4().hex, question_preamble=dialect.question_preamble,
                question_meta_key=dialect.question_meta_key)
        except ValueError as exc:
            self.log(f"elicitation not representable, declined: {exc}")
            return {"action": "decline"}
        schema = params.get("requestedSchema") if isinstance(params.get("requestedSchema"), dict) else {}
        decision, answers = await self._ask_human(req)
        return requests.elicitation_response(decision, answers, schema)

    async def _ask_question(self, params: dict) -> dict:
        """A question the agent raised as a permission request: the option the operator named is selected, anything
        else cancels it (`requests.question_response`). One the lane cannot represent is cancelled, never answered."""
        try:
            req = requests.request_from_question(params, uuid.uuid4().hex)
        except ValueError as exc:
            self.log(f"question not representable, cancelled: {exc}")
            return requests.permission_response(None)
        decision, answers = await self._ask_human(req)
        return requests.question_response(decision, answers, list(params.get("options") or []))

    async def _ask_human(self, req: requests.RuntimeRequest) -> tuple[str, dict | None]:
        """A human question: opened on the daemon's request lane FIRST (so an answer always has a request to
        land on), then posted as `input.request` rounds, then awaited — `(decision, answers)`. An interrupt/close
        intent answers it `cancel` and withdraws it (`runtime.request_cancel`); so does this handler being
        cancelled."""
        cancel: asyncio.Future = asyncio.get_running_loop().create_future()
        self._elicit_cancels[req.id] = cancel
        self._open_inputs += 1
        self._poke()
        opened = False
        try:
            self.emit(self.norm.user_input_request(req, status="in_progress"))
            decision, answers = "cancel", None
            try:
                await self._retrying("runtime.request_open",
                                     {"session_id": self.spec.session_id, "request": req.to_wire()}, CALL_BUDGET_S)
                opened = True
            except agent_socket.AgentSocketRefused as exc:
                self.log(f"user_input request {req.id} cancelled: {exc}")
            if opened:
                await self._post_questions(req)
                waiter = asyncio.ensure_future(self._await_decision(req.id))
                try:
                    await asyncio.wait({waiter, cancel}, return_when=asyncio.FIRST_COMPLETED)
                    decided = waiter.done()
                finally:
                    if not waiter.done():
                        waiter.cancel()
                if decided:
                    try:
                        decision, answers = waiter.result()
                    except (agent_socket.AgentSocketRefused, ValueError) as exc:
                        self.log(f"user_input request {req.id} cancelled: {exc}")
                    if decision.startswith("accept"):
                        # The receipt: the answer goes to the agent as this request's result, now.
                        self.emit(self.norm.answer_received("delivered", via="request", request_id=req.id))
                else:
                    self.log(f"user_input request {req.id} cancelled by an operator intent")
                    await self._request_cancel(req.id)
            self.emit(self.norm.user_input_request({**req.to_wire(), "decision": decision}, status="completed"))
            return decision, answers
        except asyncio.CancelledError:
            if opened:
                self._spawn_task(self._request_cancel(req.id))
            raise
        finally:
            self._elicit_cancels.pop(req.id, None)
            self._open_inputs -= 1
            self._poke()

    async def _post_questions(self, req: requests.RuntimeRequest) -> None:
        for q in req.questions:
            question = {"question": q.get("question", ""), "header": q.get("header", ""),
                        "options": [o.get("label", "") for o in q.get("options") or ()],
                        "multi_select": bool(q.get("multi_select"))}
            try:
                await self.call("input.request", question, CALL_BUDGET_S)
            except agent_socket.AgentSocketError as exc:
                self.log(f"input.request not posted: {exc}")

    async def _await_decision(self, request_id: str) -> tuple[str, dict | None]:
        while True:
            res = await self._retrying("runtime.request_wait", {"request_id": request_id}, WAIT_BUDGET_S)
            if isinstance(res, dict) and "decision" in res:
                break
        answers = res.get("answers") if isinstance(res.get("answers"), dict) else None
        return requests.validate_decision(res["decision"]), answers

    async def _request_cancel(self, request_id: str) -> None:
        try:
            await self.call("runtime.request_cancel", {"request_id": request_id}, CALL_BUDGET_S)
        except agent_socket.AgentSocketError as exc:
            self.log(f"runtime.request_cancel {request_id} not recorded: {exc}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--launch":
        print("usage: jaah-acp-host --launch <launch.json>", file=sys.stderr)
        return 2
    try:
        spec = launch.load(args[1])
    except launch.LaunchRefused as exc:
        print(f"jaah-acp-host: {exc}", file=sys.stderr, flush=True)
        return 2
    # Only the signals this platform has: Windows has no SIGHUP (its console close arrives as SIGBREAK, which
    # Python cannot catch for a pane anyway), and a missing name must not stop the host at start.
    for signum in (getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)):
        signal.signal(signum, _exit_on_signal)
    try:
        return asyncio.run(Host(spec).serve())
    finally:
        remove_run_dir(spec)


if __name__ == "__main__":
    raise SystemExit(main())
