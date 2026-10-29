#!/usr/bin/python3
"""The `control` MCP server — a MODEL's typed channel to the control plane.

A non-deciding forwarder onto the daemon's vocabulary socket: no credential here, `agent_vocabulary.resolve`
judges daemon-side, and the task id is peer-derived. Only verbs carrying a `ToolSpec` are reachable.

STDOUT IS THE TRANSPORT — only JSON-RPC; a stray `print()` breaks every tool. Diagnostics go to stderr.
STDLIB ONLY: it ships in the jaahd .deb, no pip dependency.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ── locating the daemon tree ────────────────────────────────────────────────────────────────────────────
# `${CLAUDE_PLUGIN_ROOT}` is either the source tree (three parents up is the daemon) or a copy under
# `plugins/cache/` with no daemon tree above it — hence the packaged fallback. Import, never copy.
_PACKAGED_DAEMON_DIR = "/usr/lib/jaahd/apps/jaahd"


def _daemon_dir() -> str:
    """The `apps/jaahd` directory holding `agent_socket`, or "". `$JAAH_MCP_DAEMON_DIR` (tests) wins."""
    candidates = [
        os.environ.get("JAAH_MCP_DAEMON_DIR") or "",
        str(Path(__file__).resolve().parent.parent.parent),  # mcp/jaah/ -> mcp/ -> apps/jaahd/
        # Off Linux the installed standard is the bundle, not the package path (`runner_env.LIB_ROOT_ENV`,
        # exported into every runner and console pane there); unset on Linux.
        str(Path(os.environ["JAAH_LIB_ROOT"]) / "apps" / "jaahd") if os.environ.get("JAAH_LIB_ROOT") else "",
        _PACKAGED_DAEMON_DIR,
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "agent_socket.py").is_file():
            return candidate
    return ""


_DAEMON_DIR = _daemon_dir()
if _DAEMON_DIR and _DAEMON_DIR not in sys.path:
    # Append, not insert(0): its flat module names must not shadow the stdlib.
    sys.path.append(_DAEMON_DIR)

try:
    import agent_socket
    import agent_vocabulary
except ImportError as exc:
    # Fail loud: an empty `tools/list` would leave a runner no way to report and no reason why.
    print(
        f"jaah-mcp: cannot import the jaahd tree (looked in {_DAEMON_DIR or 'no candidate'}, "
        f"packaged path {_PACKAGED_DAEMON_DIR}): {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1)


# ── protocol constants ──────────────────────────────────────────────────────────────────────────────────
# Only the `tools` primitive is served, stable across spec revisions, so the requested version is echoed;
# this is the answer when the client names none.
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "control"

# Added to a verb's `timeout_s` (the daemon's CP budget) for our socket read: waiting exactly that long
# leaves nothing for the daemon's own work (e.g. `artifact.upload`'s multipart build) and reads `unavailable`.
_CLIENT_HEADROOM_S = 30.0

# Channel-level doctrine the model reads once per session; per-tool text lives in the `ToolSpec`s and
# when-to-report in the runner charter.
_INSTRUCTIONS = """\
These tools are how you report to the control plane. There is no control-plane URL, no token, and
nothing to curl.

Your task id is supplied by the node daemon from this session's own identity. You never pass one, and you
cannot: an argument naming a task is refused outright, which is what makes it impossible to report onto
another runner's task. $CONTROL_TASK_ID is for your own reference only.

A failed call comes back as a tool error naming one coarse reason: `not_a_runner` (the daemon cannot tell
which task this session is, so no tool here will work and there is no DB-direct report left to make),
`forbidden_arg`, `bad_request`, `unknown_verb`, `malformed_call` (your tool call's parameters were not
parsed as separate arguments — one value swallowed the ones after it, so they arrived absent — or named an
argument the tool does not have; this reason CARRIES an explanation naming the argument and what was lost or
accepted, so read it and re-send the whole call rather
than retrying it as sent), `unavailable` (the daemon or the control plane could not
be reached — the call may not have happened, and may be retried as sent), `rejected` (the control plane
received it and REFUSED it, carrying the HTTP status and the control plane's own detail text — FIX the
request; retried unchanged it earns the same refusal), `busy` (retry), `nodevault_key_missing` (a NodeVault
credential this node does not hold the key for yet — its delivery has been started; retry in a few
minutes), `nodevault_open_failed` (a NodeVault credential that would not open on this node — report it,
do not retry), or `unknown_tool` (you named
something that is not a tool here — the verb spelling with dots is the usual cause; use the tool names as
listed).

A successful call returns the endpoint's own JSON. When you are unsure whether a write landed, read your
row back with task_get first — but read an empty result correctly: it resolves only a still-open row, so
`null` means the task is no longer open, i.e. the settle DID land. It is never a reason to retry.

Hooks, gates and repo scripts reach the same daemon over the same socket with the `jaah-call` CLI. That
is their channel, not yours, and it is not deprecated — do not migrate a script off it.
"""


def _log(message: str) -> None:
    """Diagnostics to stderr. NEVER stdout — see the module docstring."""
    print(f"jaah-mcp: {message}", file=sys.stderr, flush=True)


# ── JSON-RPC plumbing ───────────────────────────────────────────────────────────────────────────────────
# MCP stdio is newline-delimited JSON (no Content-Length framing).

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
# A fault on our side — never reported as `_INVALID_REQUEST`.
_INTERNAL_ERROR = -32603


def _write(message: dict, out=None) -> None:
    """One JSON-RPC message, one line — no literal newline, or it reads as several messages."""
    stream = sys.stdout if out is None else out
    stream.write(json.dumps(message, separators=(",", ":")) + "\n")
    stream.flush()


def _result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_failure(request_id, reason: str) -> dict:
    """A tool that ran and FAILED is a `result` with `isError`, not a JSON-RPC `error`.

    Only `isError` content reaches the model as the answer; a JSON-RPC error may be swallowed."""
    return _result(request_id, {"content": [{"type": "text", "text": reason}], "isError": True})


# ── the tools ───────────────────────────────────────────────────────────────────────────────────────────

# The session's project-memory switch — literal twin of `project_memory_bundle.ENV_BUNDLE` (that module is
# not a cheap import), exported only by an ON launch and read by the memory hooks too. Pinned by a test.
_MEMORY_ENV = "JAAH_PROJECT_MEMORY"


def _memory_armed() -> bool:
    """Is this session's project memory ON? Read per request: the server runs inside the session."""
    return bool(os.environ.get(_MEMORY_ENV))


def tool_definitions() -> list[dict]:
    """Every model-facing verb this session may call as an MCP tool descriptor, straight off the vocabulary
    table: the memory tools only when memory is ON (`agent_vocabulary.tool_verbs`). A branch rule goes in the
    description, never as a top-level `oneOf`: a Claude Code that meets one drops the tool (`one_of_rule`)."""
    armed = _memory_armed()
    tools = []
    for verb in agent_vocabulary.tool_verbs(armed):
        schema = agent_vocabulary.tool_input_schema(verb, memory_armed=armed)
        description = verb.tool.summary
        if schema.pop("oneOf", None):
            description = f"{description}\n\n{agent_vocabulary.one_of_rule(verb)}"
        tools.append({"name": agent_vocabulary.tool_name(verb.name), "description": description,
                      "inputSchema": schema})
    return tools


def call_tool(name: object, arguments: object, *, call=None) -> tuple[bool, str]:
    """Perform one tool call. Returns `(ok, text)`; `text` is the endpoint's JSON on success and the
    coarse refusal reason on failure.

    `call` is injectable for tests. No argument validation here: `agent_vocabulary.resolve` is the single
    judge, daemon-side."""
    caller = agent_socket.call if call is None else call
    armed = _memory_armed()
    verb = agent_vocabulary.BY_TOOL.get(name) if isinstance(name, str) else None
    if verb is None or (verb.tool.memory_only and not armed):
        # Not `_METHOD_NOT_FOUND`: this is the answer to a legitimate question. A memory tool in a memory-OFF
        # session answers the same, matching `tools/list`.
        return False, f"unknown_tool: no such tool {name!r}"
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return False, f"bad_request: arguments must be an object, got {type(arguments).__name__}"
    if verb.tool.memory_required:
        # The arm travels as a reserved key `resolve` consumes (`agent_vocabulary.MEMORY_ARMED`); a model's
        # own copy is never trusted either way.
        arguments = {k: v for k, v in arguments.items() if k != agent_vocabulary.MEMORY_ARMED}
        if armed:
            arguments[agent_vocabulary.MEMORY_ARMED] = True
    try:
        result = caller(verb.name, arguments, timeout=verb.timeout_s + _CLIENT_HEADROOM_S)
    except agent_socket.AgentSocketRefused as exc:
        # The vocabulary's judgement, verbatim.
        return False, exc.reason
    except agent_socket.AgentSocketUnavailable as exc:
        # The call MAY have happened; a model must not assume it did not.
        return False, f"unavailable: {exc}"
    if verb.name == _PEER_ARTIFACT_VERB:
        try:
            result = _download_peer_artifact(result)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # Nothing was written (`_download_peer_artifact` unlinks a partial file); safe to retry.
            return False, f"unavailable: could not download the artifact: {exc}"
    return True, json.dumps(result, separators=(",", ":"), default=str)


# ── the one tool with a client-side step ──────────────────────────────────────────────────────────────────
# `artifact.read` answers a presigned GET. THIS process runs as the runner's own uid, so it — not the
# daemon — streams the bytes into the runner's report directory: no privileged process ever writes into a
# runner's tree, and a dynamic-env runner's own uid is covered with no special case. The URL is a bearer
# credential for minutes and is dropped from what the model sees.
_PEER_ARTIFACT_VERB = "artifact.read"
# The daemon's streamed ceiling (`control_plane_api.MAX_STREAM_BYTES`), mirrored: one artifact's ceiling. Safe
# because the download streams to disk in `_CHUNK`s (a test pins the pair).
_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 300.0
_CHUNK = 1024 * 1024


def _artifact_dir() -> Path:
    """`$JAAH_REPORT_DIR/peer-tasks` — never the worktree, so a download is never something to commit or
    clean up. Without one (an interactive session), a fresh private `mkdtemp`: a predictable shared `/tmp`
    name could be pre-created by another local uid."""
    global _FALLBACK_DIR
    root = os.environ.get("JAAH_REPORT_DIR")
    if not root:
        # Once per process. A vendor CLI that filters an MCP server's environment lands here.
        _FALLBACK_DIR = _FALLBACK_DIR or tempfile.mkdtemp(prefix="jaah-peer-artifacts-")
        root = _FALLBACK_DIR
    return Path(root) / "peer-tasks"


_FALLBACK_DIR = ""


def _download_peer_artifact(answer: object, *, opener=None) -> dict:
    """Stream the answer's `download_url` to `<artifact dir>/task-<id>/<artifact id>_<name>` and return the
    answer with `path` in place of the URL. Raises `ValueError` on a malformed answer or an oversize body,
    `OSError` on a transfer or write failure; a partial file is removed either way."""
    if not isinstance(answer, dict) or not isinstance(answer.get("download_url"), str):
        raise ValueError("the control plane answered with no download url")
    url = answer["download_url"]
    # Presigned S3 GETs are https; `urlopen` would also follow `file:`/`ftp:`/`data:` — refuse them.
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("the control plane answered with a non-https download url")
    meta = {k: v for k, v in answer.items() if k != "download_url"}
    task_id, artifact_id = meta.get("task_id"), meta.get("id")
    if not isinstance(task_id, int) or not isinstance(artifact_id, int):
        raise ValueError("the control plane answered with no task or artifact id")
    if isinstance(meta.get("size_bytes"), int) and meta["size_bytes"] > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"the artifact is over the {_MAX_ARTIFACT_BYTES} byte cap")
    # A runner-authored filename: basename only, and only filename-safe characters.
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(str(meta.get("filename") or ""))).lstrip(".")
    target_dir = _artifact_dir() / f"task-{task_id}"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / f"{artifact_id}_{name or 'artifact'}"
    partial = target.with_name(f"{target.name}.{os.getpid()}.part")
    opener = opener or urllib.request.urlopen
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_S
    written = 0
    try:
        # `O_EXCL | O_NOFOLLOW`: never write through a planted symlink or into a file that already exists.
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
        # The descriptor is wrapped FIRST so a failing `opener` cannot leak it.
        with os.fdopen(fd, "wb") as handle, opener(url, timeout=_DOWNLOAD_TIMEOUT_S) as response:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_ARTIFACT_BYTES:
                    raise ValueError(f"the artifact is over the {_MAX_ARTIFACT_BYTES} byte cap")
                if time.monotonic() > deadline:
                    raise OSError(f"the download took longer than {_DOWNLOAD_TIMEOUT_S:.0f}s")
                handle.write(chunk)
        os.replace(partial, target)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    meta["path"] = str(target)
    meta["size_bytes"] = written
    return meta


# ── the request loop ────────────────────────────────────────────────────────────────────────────────────

def handle(message: dict) -> dict | None:
    """One request in, one response out — or None for a notification, which by JSON-RPC takes no reply.

    Pure apart from the socket call under `tools/call`."""
    request_id = message.get("id")
    method = message.get("method")
    is_notification = "id" not in message

    if "method" not in message:
        # A response; this server initiates nothing, so drop it.
        return None

    if is_notification:
        # Before dispatch: JSON-RPC forbids replying to a message with no id, known method or not.
        return None

    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(request_id, {
            # A non-empty string is echoed; anything else takes the default.
            "protocolVersion": (requested.strip() if isinstance(requested, str) and requested.strip()
                                else _DEFAULT_PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _SERVER_NAME, "version": _version()},
            "instructions": _INSTRUCTIONS,
        })
    if method == "tools/list":
        return _result(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, _INVALID_PARAMS, "params must be an object")
        ok, text = call_tool(params.get("name"), params.get("arguments"))
        if not ok:
            return _tool_failure(request_id, text)
        return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
    if method == "ping":
        return _result(request_id, {})
    return _error(request_id, _METHOD_NOT_FOUND, f"unknown method: {method}")


def _version() -> str:
    """The plugin's version from the manifest beside this file; "0" when unreadable (cosmetic, never fatal)."""
    manifest = Path(__file__).resolve().parent / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text()).get("version") or "0")
    except (OSError, ValueError, AttributeError):
        return "0"


def serve(stdin=None, stdout=None) -> int:
    """Read requests until EOF. Streams are injectable for tests."""
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    while True:
        # `readline()`, not `for line in source`: iteration read-ahead can strand a request on a pipe
        # until the next one arrives. "" is EOF.
        line = source.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write(_error(None, _PARSE_ERROR, "malformed JSON"), sink)
            continue
        if not isinstance(message, dict):
            _write(_error(None, _INVALID_REQUEST, "a request must be an object"), sink)
            continue
        try:
            response = handle(message)
            # Inside the guard: serializing can raise too, and one bad request must not end the session.
            if response is not None:
                _write(response, sink)
        except Exception as exc:  # noqa: BLE001
            # Logged in full, answered coarsely; the loop continues.
            _log(f"{message.get('method')!r} raised {type(exc).__name__}: {exc}")
            if "id" in message:
                try:
                    _write(_error(message.get("id"), _INTERNAL_ERROR, f"{type(exc).__name__}"), sink)
                except Exception as report_exc:  # noqa: BLE001
                    # The stream is gone; stop rather than spin.
                    _log(f"cannot write to stdout, stopping: {report_exc}")
                    return 1


if __name__ == "__main__":
    raise SystemExit(serve())
