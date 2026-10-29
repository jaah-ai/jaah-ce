"""Client for the node daemon's local vocabulary socket — the daemon's own client, loaded by path ON FIRST USE.

The protocol and the wire client live in `apps/jaahd/agent_socket.py`; this facade loads that module
through `tools/jaahd_module.py` the first time a call needs it, so a tools script, hook or gate keeps writing
`import agent_socket` and `except agent_socket.AgentSocketRefused`. LAZY, and that is load-bearing: the
daemon's client imports its `environment` and `host_platform` siblings (~40 ms cold), and `runner_flags`
imports this module on every Read hook's path — `read_once_guard`'s end-to-end budget is 100 ms (R4.5).
A hook that never dials the socket must pay nothing for it. STDLIB ONLY at import.

The two failure kinds are this module's own classes (they must exist before the daemon's module does) and
`call` translates the daemon's into them; they must never be conflated: `AgentSocketUnavailable` (no answer —
a caller holding an independent credential MAY fall back, as `control_plane_client.agent_api_request` does)
versus `AgentSocketRefused` (the daemon said no — falling back would make every rule in
`apps/jaahd/agent_vocabulary.py` advisory). A daemon client that will not LOAD (no daemon tree beside `tools/`,
a run root the daemon refuses) is the first kind: no socket transport. On Windows the endpoint is a named
pipe, not an AF_UNIX socket (Windows Python has no `socket.AF_UNIX`; `jaah-forge`'s governance lookup died
before every PR on a real PC) — one more reason the daemon's client is the only one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

# Loaded by path too (the suites): then nothing put tools/ on sys.path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import jaahd_module  # noqa: E402 — sibling module

_DAEMON_MODULE = "_jaahd_agent_socket"


class AgentSocketError(Exception):
    """Base of the two failure kinds below."""


class AgentSocketUnavailable(AgentSocketError):
    """No answer from the daemon (absent, refused, timed out — or its client would not load). A caller holding
    an independent credential MAY fall back; no policy decision is being undermined."""


class AgentSocketRefused(AgentSocketError):
    """The daemon answered no. NEVER fall back to a credential on this."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the node daemon refused this call: {reason}")
        self.reason = reason


def _client() -> ModuleType:
    """The daemon's client module, loaded once; a load failure is `AgentSocketUnavailable` (no transport)."""
    try:
        return jaahd_module.load("agent_socket.py", _DAEMON_MODULE, siblings=True)
    except Exception as exc:  # noqa: BLE001 — ImportError (no daemon tree), or the daemon refusing its run root
        raise AgentSocketUnavailable(f"the daemon's socket client is not loadable here ({exc!r})") from exc


def available(socket_path: str | None = None) -> bool:
    """Shape check, not a permission check: a caller outside group `jaah` sees True, then gets
    `AgentSocketUnavailable` from `call` — the same branch either way. No client, no socket."""
    try:
        return bool(_client().available(socket_path))
    except AgentSocketUnavailable:
        return False


def call(verb: str, args: dict | None = None, *, socket_path: str | None = None,
         timeout: float | None = None) -> Any:
    """Ask the daemon to perform `verb`; return its `result`, or raise one of the two kinds above. Never
    returns a default on failure — a failed call must not be mistakable for an empty result."""
    client = _client()
    try:
        return client.call(verb, args, socket_path=socket_path, timeout=timeout)
    except client.AgentSocketRefused as exc:
        raise AgentSocketRefused(exc.reason) from exc
    except client.AgentSocketUnavailable as exc:
        raise AgentSocketUnavailable(str(exc)) from exc


def __getattr__(name: str) -> Any:
    """`TIMEOUT_S`, `SOCKET_PATH` and `_socket_path` are the daemon module's own, read through it on demand
    (a test pins the budget or the resolution; nothing on a hook's path reads them)."""
    if name in ("TIMEOUT_S", "SOCKET_PATH", "_socket_path"):
        return getattr(_client(), name)
    raise AttributeError(name)
