#!/usr/bin/python3 -I
"""agents/pi_rpc.py — the `pi` that `pi-acp` spawns for a Pi runner: write Pi's files, then exec the real `pi`.

pi-acp runs `$PI_ACP_PI_COMMAND --mode rpc --no-themes [--session <file>]`; `agents/pi.py` points that variable here.
This process is the engine's own (a child of pi-acp, under `jaah-acp-host`), so it runs as the runner uid and sees
the engine's paths, on a static node and inside a MicroVM alike. It reads the `JAAH_PI_LAUNCH` document
(`agents/pi.py::PiAdapter.acp_launch`) and writes, in Pi's config home (`PI_CODING_AGENT_DIR`, else `~/.pi/agent`):

  * `settings.json` — the document's `settings` merged over what is there (Pi keeps its own preferences there);
  * `auth.json` (0600) — the `anthropic` provider's key as the broker-client `!command`, never the key itself;
  * `mcp.json` — the control server (ACP `McpServer` entries, translated), `direct` exposure.

`auth.json` and `mcp.json` are AUTHORITATIVE: written whole from the document, so nothing another runner of the same
uid planted there (a server, an endpoint, a second credential) survives into this run. Each file is replaced
atomically and holds the same content for every run of the account, so concurrent runs cannot tear one. Then it
execs `pi` with pi-acp's arguments plus `PI_FLAGS` (no extensions but the built-in MCP one and the fleet's
`pi_extension/jaah-harness.ts`, no project-trusted resources from the worktree), the host's charter file
(`--append-system-prompt`, `$JAAH_PI_SYSTEM_PROMPT`) and `--provider <provider> --model <model>`.

pi-acp discards pi's stderr, so this process first sends its own (and with it pi's, inherited across the exec) to
the host's relay file (`JAAH_ENGINE_STDERR`, `acp/host.py`), whose tail names a start failure in the break. Before
anything else it proves Pi's config home and its `sessions/` writable: pi dies on an unwritable one (EACCES on its
`mkdir`), which pi-acp reports only as "Cannot call write after a stream was destroyed", so a failed check is said
plainly and pi is not started. A failure to write the run's files is reported and pi starts anyway: pi-acp then
reports the missing key or model as an ACP error the host records. Stdlib only, run with `python3 -I`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

if __package__:
    from . import staged_write
else:  # run by path under `-I`: this file's own directory is not on `sys.path`
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))   # last: shadowing nothing
    import staged_write  # type: ignore[no-redef]

LAUNCH_ENV = "JAAH_PI_LAUNCH"
#: `acp/host.py::ENGINE_STDERR_ENV` (this script imports nothing of the daemon's).
STDERR_ENV = "JAAH_ENGINE_STDERR"
#: The exit status of a refused start (EX_CANTCREAT).
EXIT_UNWRITABLE = 73
AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
PI_BINARY = "pi"
#: The fleet's hook layer and question tool inside Pi (`pi_extension/jaah-harness.ts`): beside this file in the
#: packaged, root-owned tree (a toolchain share in a MicroVM guest), never under the worktree.
EXTENSION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_extension", "jaah-harness.ts")
#: Extensions run inside Pi's process, so none load but the built-in MCP support the control server needs and the
#: fleet's own, and the worktree's trust-gated `.pi/` (its extensions, packages, settings) is ignored (Pi's CLI doc).
PI_FLAGS: tuple[str, ...] = ("--no-extensions", "--extension", "builtin:mcp", "--extension", EXTENSION, "--no-approve")
#: The charter file the ACP host writes before `session/new` (`AcpDialect.system_prompt_file_env`, `acp/host.py`).
SYSTEM_PROMPT_ENV = "JAAH_PI_SYSTEM_PROMPT"
#: Files written whole from the document; the others are merged over what is there.
AUTHORITATIVE: frozenset[str] = frozenset({"auth.json", "mcp.json"})


def agent_dir(environ: dict) -> str:
    """Pi's config home, as Pi resolves it."""
    return environ.get(AGENT_DIR_ENV) or os.path.join(os.path.expanduser("~"), ".pi", "agent")


def relay_stderr(environ: dict) -> None:
    """Point fd 2 at the host's relay file, when it named one: pi inherits it across the exec."""
    path = environ.get(STDERR_ENV)
    if not path:
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError:
        return
    os.dup2(fd, 2)
    os.close(fd)


def unwritable(directory: str) -> str:
    """Why Pi could not keep its files in `directory` (its config home, and `sessions/` where every session is
    written), or "". Creates both as Pi would, then proves `sessions/` takes a file."""
    sessions = os.path.join(directory, "sessions")
    try:
        os.makedirs(sessions, mode=0o700, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=sessions, prefix=".pi_rpc-probe-")
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc.strerror or exc} ({exc.filename or sessions})"
    return ""


def mcp_entry(server: dict) -> dict:
    """An ACP stdio `McpServer` (`env` as `[{name, value}]`) as a Pi `mcp.json` server, its tools declared."""
    env = {str(e.get("name")): str(e.get("value")) for e in server.get("env") or [] if isinstance(e, dict)}
    entry: dict = {"command": str(server["command"]), "args": [str(a) for a in server.get("args") or []],
                   "exposure": "direct"}
    if env:
        entry["env"] = env
    return entry


def files(document: dict) -> dict[str, tuple[dict, int]]:
    """`{file name: (patch, mode)}` for Pi's config home — each patch merged at the top level over the file."""
    key = str(document.get("key_command") or "")
    servers = {str(s["name"]): mcp_entry(s) for s in document.get("mcp_servers") or []
               if isinstance(s, dict) and s.get("name") and s.get("command")}
    # The two authoritative files are always written, empty when the run has none, so no earlier content survives.
    return {
        "settings.json": (dict(document.get("settings") or {}), 0o644),
        "auth.json": ({str(document["provider"]): {"type": "api_key", "key": key}} if key else {}, 0o600),
        "mcp.json": ({"mcpServers": servers}, 0o644),
    }


def merged(path: str, patch: dict) -> dict:
    """`patch` over the JSON object at `path` (one level deep: a key in both that holds objects is merged)."""
    try:
        with open(path, encoding="utf-8") as handle:
            current = json.load(handle)
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    return current


def write(directory: str, name: str, content: dict, mode: int) -> None:
    """Replace `<directory>/<name>` atomically with `content`, created at `mode` (never wider)."""
    staged_write.replace_file(os.path.join(directory, name), json.dumps(content, indent=2) + "\n", mode=mode)


def converge(directory: str, document: dict) -> None:
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for name, (patch, mode) in files(document).items():
        content = patch if name in AUTHORITATIVE else merged(os.path.join(directory, name), patch)
        write(directory, name, content, mode)


def system_prompt_args(environ: dict) -> list[str]:
    """`--append-system-prompt <file>` for the host's charter file, when it is there: Pi reads a path that is not an
    existing file as the text itself."""
    path = environ.get(SYSTEM_PROMPT_ENV) or ""
    if path and not os.path.isfile(path):
        print(f"pi_rpc: the charter file {path} (${SYSTEM_PROMPT_ENV}) is not there; pi starts without it",
              file=sys.stderr, flush=True)
        return []
    return ["--append-system-prompt", path] if path else []


def pi_argv(argv: list[str], document: dict, environ: dict | None = None) -> list[str]:
    """pi-acp's arguments, `PI_FLAGS`, the host's charter file, then the run's provider and model (none when the
    launch named no model)."""
    model = str(document.get("model") or "")
    pinned = ["--provider", str(document["provider"]), "--model", model] if model else []
    return [PI_BINARY, *argv, *PI_FLAGS, *system_prompt_args(environ or {}), *pinned]


def main(argv: list[str], environ: dict | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    relay_stderr(environ)
    directory = agent_dir(environ)
    why = unwritable(directory)
    if why:
        print(f"pi_rpc: Pi cannot start: its config home {directory} (or its sessions/) is not writable by uid "
              f"{os.getuid()}: {why}", file=sys.stderr, flush=True)
        return EXIT_UNWRITABLE
    try:
        document = json.loads(environ.get(LAUNCH_ENV) or "")
        if not isinstance(document, dict):
            raise ValueError("not a JSON object")
    except ValueError as exc:
        print(f"pi_rpc: {LAUNCH_ENV} is unreadable ({exc}); starting pi without the run's files", file=sys.stderr)
        document = {}
    if document:
        try:
            converge(directory, document)
        except (OSError, KeyError, TypeError) as exc:
            print(f"pi_rpc: could not write Pi's config ({type(exc).__name__}: {exc}); starting pi anyway",
                  file=sys.stderr)
    args = (pi_argv(argv, document, environ) if document.get("provider")
            else [PI_BINARY, *argv, *PI_FLAGS, *system_prompt_args(environ)])
    os.execvp(args[0], args)
    return 127  # unreachable: execvp raises on failure


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
