"""acp/lifecycle.py — the lifecycle hooks the ACP host runs in front of an engine that does not run them itself.

A row's `AcpDialect.host_lifecycle` names the hook events (`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`,
`PreCompact`); the launch's `host_hooks` holds their entries (`agents/base.py::host_lifecycle_dispatch`, carried in
launch.json as `runtime.lifecycle_hooks`), the same file shape the engine's in-band dispatch reads. Each event runs
through THE one neutral dispatcher, `hook_bridge_core.py dispatch --hooks-stdin`, as a child with the engine's
own env and the worktree as its cwd, so a hook sees what it would running inside the engine. The hooks stay in this
process's memory and reach the dispatcher on its stdin: no file the engine (the same uid) could rewrite selects them. When the host calls each event is
`acp/host.py`'s: once before `session/new`, before each prompt it sends, at each turn end, on a compaction
(`events.compaction_observed`), once at the end.

Fail-open: a dispatcher that cannot run, overruns `DISPATCH_BUDGET_S` or answers nonsense is an `allow` with no
context, logged. Stdlib only, no daemon imports beyond the host's own."""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import sys
from pathlib import Path
from typing import Awaitable, Callable

#: The neutral dispatcher, by its installed path (`<hub>/tools/`, beside `apps/`).
DISPATCHER = Path(__file__).resolve().parents[3] / "tools" / "hook_bridge_core.py"
#: The outer wall clock on one event's dispatch. Each entry's own timeout is the dispatcher's; this bounds a dispatcher
#: that hangs (the `Stop` quick gate's 60s is the longest entry today).
DISPATCH_BUDGET_S = 600.0
#: The budget at the attempt's end (`SessionEnd`, and the wait for a running `PreCompact`): the engine has stopped and
#: the attempt's report waits on it.
END_BUDGET_S = 30.0

#: `(argv, stdin, env, cwd, timeout) -> stdout`: the child runner, injectable for tests.
Runner = Callable[[list, bytes, dict, str, float], Awaitable[bytes]]


async def run_child(argv: list, stdin: bytes, env: dict, cwd: str, timeout: float) -> bytes:
    """`argv` with `stdin`, its stdout; the child is killed past `timeout` (TimeoutError). Each hook's own process
    group is the dispatcher's to kill on its own timeout (`hook_bridge_core.run_hook`)."""
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=cwd or None, env=env)
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        raise
    return stdout


@functools.lru_cache(maxsize=1)
def _core():
    """The hook dispatcher `hook_bridge_core.py`, loaded by its installed path (a hook script, not a package
    module)."""
    spec = importlib.util.spec_from_file_location("jaah_hook_bridge_core", DISPATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capped(text: str) -> str:
    """`text` within the hook-context budget (`hook_bridge_core.capped`, its one home)."""
    try:
        return _core().capped(text)
    except Exception:  # noqa: BLE001 — no dispatcher in this install: nothing else is cut either
        return text


def drain_spool(path: str) -> str:
    """The context spool at `path`, taken whole and read to a bound (`hook_bridge_core.drain_spool`, its one home);
    "" when none."""
    if not path:
        return ""
    try:
        core = _core()
        return core.drain_spool(path, max_bytes=core.MAX_CONTEXT_CHARS * 4)
    except Exception as exc:  # noqa: BLE001 — a dispatcher missing from this install costs a hint, never the run
        print(f"jaah-acp-host: hook context not drained ({type(exc).__name__})", file=sys.stderr, flush=True)
        return ""


def _verdict(decision: str = "allow", reason: str = "", context: str = "") -> dict:
    return {"decision": decision, "reason": reason, "context": context}


class HostLifecycle:
    """The host's lifecycle runner for one attempt. `events` is the row's `host_lifecycle`; `hooks` the launch's
    `host_hooks` (None or empty = nothing runs, whatever the row says)."""

    def __init__(self, events: tuple[str, ...] = (), hooks: dict | None = None, *, env: dict | None = None,
                 cwd: str = "", log: Callable[[str], None] = print, runner: Runner = run_child) -> None:
        self.events = frozenset(events) if hooks else frozenset()
        self.hooks = dict(hooks or {})
        self.env = dict(env or {})
        self.cwd = cwd
        self.log = log
        self.runner = runner

    def runs(self, event: str) -> bool:
        return event in self.events

    async def fire(self, event: str, session_id: str, *, budget: float = DISPATCH_BUDGET_S, **fields: object) -> dict:
        """Run `event`'s entries through the dispatcher; its `{"decision", "reason", "context"}`, or an empty allow
        when the host does not run `event`. `fields` join the hook payload (`prompt`, `source`, `reason`,
        `stop_hook_active`, `last_assistant_message`, `trigger`)."""
        if not self.runs(event):
            return _verdict()
        payload = {"session_id": session_id, "transcript_path": "", "cwd": self.cwd, **fields}
        argv = [sys.executable, "-I", str(DISPATCHER), "dispatch", "--event", event, "--hooks-stdin"]
        stdin = json.dumps({"hooks": self.hooks, "payload": payload}).encode("utf-8")
        try:
            out = await self.runner(argv, stdin, self.env, self.cwd, budget)
            lines = out.decode("utf-8", "replace").strip().splitlines()
            result = json.loads(lines[-1]) if lines else None
        except (asyncio.TimeoutError, OSError, ValueError) as exc:
            self.log(f"lifecycle {event} not run ({type(exc).__name__}); allowing")
            return _verdict()
        if not isinstance(result, dict):
            self.log(f"lifecycle {event}: the dispatcher answered no verdict; allowing")
            return _verdict()
        decision = "deny" if result.get("decision") == "deny" else "allow"
        verdict = _verdict(decision, str(result.get("reason") or ""), capped(str(result.get("context") or "")))
        self.log(f"lifecycle {event} decision={decision} context_chars={len(verdict['context'])}")
        return verdict
