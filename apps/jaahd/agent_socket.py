"""Client for the daemon's local vocabulary socket — the daemon-side half.

The caller holds no credential: it names a verb from `agent_vocabulary` and the daemon, which alone can
read /etc/jaah/node.env, makes the authenticated call. Reachability is a filesystem question (group
`jaah` on a 2750 directory), not a credential one.

Wire, one line of JSON each way, sharing the credential broker's socket:
    -> {"call": "task.complete", "args": {...}}
    <- {"ok": true, "result": ...}   or  {"ok": false, "error": "not_a_runner"}

TWO FAILURE KINDS, AND CONFLATING THEM DEFEATS THE WALL. `AgentSocketUnavailable` = no answer, so a
caller holding another credential may legitimately fall back. `AgentSocketRefused` = the daemon said no,
and falling back then would make every rule in `agent_vocabulary` advisory.

`tools/agent_socket.py` is a facade that loads THIS file by path (`tools/jaahd_module.py`) and binds its
names: this is the single home, for the tools tree too."""
from __future__ import annotations

import json
import os
import socket
from typing import Any

# All host I/O belongs to the node daemon. Stdlib-only and raises nothing at import, so a session hook may import it.
import environment
import host_platform

# Shared with the credential broker — one socket, two request shapes. Overridable only for tests.
SOCKET_PATH = environment.credential_socket()

# A wedged daemon must fail fast rather than hang a hook or gate. Matches `jaah-credential`'s budget.
TIMEOUT_S = 5.0


def _socket_path(override: str | None = None) -> str:
    """The socket to talk to, resolved AT CALL TIME — never bound at import.

    Import-time binding silently ignored a `JAAH_CRED_SOCKET` set later, so a "hermetic" suite talked
    to the live daemon. Env var names are `environment.credential_socket_explicit()`'s. `SOCKET_PATH` stays
    LAST so a test that monkeypatches the attribute still wins when nothing is set in the environment."""
    return override or environment.credential_socket_explicit() or SOCKET_PATH

# A verb reply is a few hundred bytes (a settle summary more); 256 KiB still bounds a runaway stream.
_MAX_REPLY_BYTES = 262144


class AgentSocketError(RuntimeError):
    """Base: this call did not produce a result."""


class AgentSocketUnavailable(AgentSocketError):
    """No answer from the daemon — socket absent, refused, or timed out. A caller with an independent
    credential MAY fall back; there is no policy decision here to undermine."""


class AgentSocketRefused(AgentSocketError):
    """The daemon answered, and the answer was no. `reason` is the coarse wire error
    (`unknown_verb`, `bad_request`, `not_a_runner`, `unavailable`, `rejected`). NEVER fall back to a
    credential on this — see the module docstring."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the node daemon refused this call: {reason}")
        self.reason = reason


def available(socket_path: str | None = None) -> bool:
    """Is there a socket to talk to? A cheap pre-check for a caller deciding between transports.

    Not a permission check: a caller outside group `jaah` sees True here and gets EACCES from `call`,
    which lands in `AgentSocketUnavailable` either way.

    Windows: the endpoint is a named pipe, which `os.stat` would OPEN — consuming one of the daemon's pipe
    instances as a connection that never speaks. The pipe namespace is listed instead (no connect)."""
    path = _socket_path(socket_path)
    if host_platform.is_windows():
        prefix = "\\\\.\\pipe\\"
        if not path.lower().startswith(prefix):
            return False
        try:
            return path[len(prefix):].lower() in {name.lower() for name in os.listdir(prefix)}
        except OSError:
            return False
    try:
        import stat as _stat

        return _stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def call(verb: str, args: dict | None = None, *, socket_path: str | None = None,
         timeout: float | None = None, capability: str = "") -> Any:
    """Ask the daemon to perform `verb` and return its `result`. `capability`: a guest ACP host's runtime
    capability (`cred_broker._host_fence`), sent beside the args only when set.

    Raises `AgentSocketUnavailable` when we got no answer and `AgentSocketRefused` when the answer was no.
    Never returns a partial or default value: a failed call must not be mistakable for an empty result."""
    body = {"call": verb, "args": args or {}, **({"capability": capability} if capability else {})}
    return _exchange(verb, body, socket_path, timeout).get("result")


def credential(name: str, *, socket_path: str | None = None, timeout: float | None = None) -> str:
    """The credential broker's `{"credential": name}` answer (e.g. `account_api_key:<slug>`, the account this
    pane was leased): its `value`. The same two failure kinds as `call`; an empty value is a refusal. The value
    is a LIVE SECRET: the caller holds it in memory only and never logs it."""
    value = _exchange(name, {"credential": name}, socket_path, timeout).get("value")
    if not isinstance(value, str) or not value:
        raise AgentSocketRefused("empty")
    return value


def _exchange(label: str, body: dict, socket_path: str | None, timeout: float | None) -> dict:
    """One request line out, one reply object back, `ok` checked. `label` names the request in errors."""
    path = _socket_path(socket_path)
    # Per-call: a pool mutation (170s) would be abandoned at the 5s default as
    # "unreachable" while the daemon was still succeeding.
    budget = TIMEOUT_S if timeout is None else timeout
    request = json.dumps(body, separators=(",", ":")) + "\n"
    try:
        with host_platform.connect_socket(path, timeout=budget) as sock:
            sock.sendall(request.encode())
            sock.shutdown(socket.SHUT_WR)
            buf = b""
            while b"\n" not in buf and len(buf) < _MAX_REPLY_BYTES:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as exc:
        raise AgentSocketUnavailable(f"{label}: node daemon unreachable at {path}: {exc}") from exc

    if not buf:
        raise AgentSocketUnavailable(f"{label}: node daemon closed the connection without replying")
    try:
        reply = json.loads(buf.split(b"\n", 1)[0])
    except ValueError as exc:
        raise AgentSocketUnavailable(f"{label}: node daemon returned malformed JSON") from exc
    if not isinstance(reply, dict):
        raise AgentSocketUnavailable(f"{label}: node daemon returned {type(reply).__name__}, not an object")
    if reply.get("ok") is not True:
        reason = str(reply.get("error") or "unknown")
        # A refusal whose fixed explanation travels beside it (`cred_broker._NodeVaultRefused`).
        detail = reply.get("detail")
        raise AgentSocketRefused(f"{reason}: {detail}" if isinstance(detail, str) and detail else reason)
    return reply
