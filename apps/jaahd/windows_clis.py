"""`jaah-credential` and `jaah-call` on a Windows node: the same two shell-level clients the Linux node
renders into /usr/local/bin (from the node reprovision's templates, which state the contract), here
reaching the broker over its named pipe through the daemon's own client (`agent_socket`), since an
AF_UNIX socket is not the endpoint there.

The CONTRACT is the Linux scripts', unchanged: one JSON line out, one back; FAIL CLOSED — on anything but a
clean `{"ok": true, ...}` reply nothing reaches stdout and the exit is non-zero, with the reason on stderr
(a `$(jaah-credential …)` that read an empty answer as success is the failure those scripts exist to end). Both
ride `agent_socket` (`credential`/`call`): one client for the pipe, none of its own.
`jaah-call` honours `JAAH_CALL_TIMEOUT_S` (positive, finite, at most `MAX_TIMEOUT_S`; anything else fails
closed). Run by the Windows bundle's `bin` launchers, under the bundle's own Python — and by
the macOS bundle's on a macOS node, where `host_platform.connect_socket` dials the AF_UNIX socket instead.
"""
from __future__ import annotations

import json
import math
import os
import sys

import agent_socket

MAX_TIMEOUT_S = 600.0


def _fail(tool: str, message: str) -> int:
    print(f"{tool}: {message}", file=sys.stderr)
    return 1


def credential_main(argv: list[str]) -> int:
    """`jaah-credential <name>`: the credential's value on stdout, or nothing and a non-zero exit."""
    if len(argv) != 1 or not argv[0]:
        return _fail("jaah-credential", "usage: jaah-credential <name>")
    try:
        value = agent_socket.credential(argv[0])
    except agent_socket.AgentSocketError as exc:
        return _fail("jaah-credential", str(exc))
    sys.stdout.write(value)
    return 0


def _timeout() -> float:
    raw = os.environ.get("JAAH_CALL_TIMEOUT_S", "")
    if not raw.strip():
        return agent_socket.TIMEOUT_S
    value = float(raw)  # ValueError: not a number
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_S:
        raise ValueError(f"JAAH_CALL_TIMEOUT_S={raw!r} must be a finite number in (0, {MAX_TIMEOUT_S:g}]")
    return value


def call_main(argv: list[str]) -> int:
    """`jaah-call <verb> [json-args]`: the verb's result as JSON on stdout, or nothing and a non-zero exit."""
    if not argv or len(argv) > 2 or argv[0].startswith("-"):
        return _fail("jaah-call", "usage: jaah-call <verb> [json-args] — the verbs: apps/jaahd/agent_vocabulary.py")
    try:
        timeout = _timeout()
        args = json.loads(argv[1]) if len(argv) == 2 else {}
    except ValueError as exc:
        return _fail("jaah-call", str(exc))
    if not isinstance(args, dict):
        return _fail("jaah-call", f"json-args must be a JSON object, got {type(args).__name__}")
    try:
        result = agent_socket.call(argv[0], args, timeout=timeout)
    except agent_socket.AgentSocketError as exc:
        return _fail("jaah-call", str(exc))
    json.dump(result, sys.stdout, separators=(",", ":"))
    return 0
