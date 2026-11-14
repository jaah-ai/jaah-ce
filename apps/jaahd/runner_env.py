"""How a process reads the runner identity (`CONTROL_TASK_ID`, `CONTROL_RUNNER_SESSION_ID`) its pane exported.

A blank value is absent, never a blank identity: a blank id would let `caller_task_id` fall through to the
privileged `host` producer sentinel.

`tools/runner_env.py` is a facade that loads THIS file by path (`tools/jaahd_module.py`) and binds its names:
this is the single home, for the tools tree too."""
from __future__ import annotations

import os
import re
import shlex


def _read(source, name: str, default: str) -> str:
    """`source[name]` if it carries a real value, else `default`.

    The one home of emptiness; whitespace counts as empty (`export X="  "` is the id lost, not set)."""
    value = (source.get(name) or "").strip()
    return value if value else default


def task_id(default: str = "", env: "dict[str, str] | None" = None) -> str:
    """`$CONTROL_TASK_ID`, or `default`; `env` overrides the ambient environment."""
    source = os.environ if env is None else env
    return _read(source, "CONTROL_TASK_ID", default)


def runner_session_id(default: str = "", env: "dict[str, str] | None" = None) -> str:
    """`$CONTROL_RUNNER_SESSION_ID`, or `default`."""
    source = os.environ if env is None else env
    return _read(source, "CONTROL_RUNNER_SESSION_ID", default)


def caller_task_id() -> str:
    """This process's `X-Agent-Task-Id` declaration for an agent-API call on the CREDENTIAL LEG (the daemon's
    own calls, `control_plane_api`; the hub's tools client on its node-token fallback): its own console task
    id inside a fleet runner's session, else the `host` producer sentinel.

    Not consulted on the socket leg, where the daemon DERIVES the header from the calling process
    (`runner_registry.resolve_peer` → `agent_vocabulary.caller_task_header`) — which is what makes
    `apps/api/app/routes/agent/auth.py::_guard_task_creating_caller` (R30) unforgeable.

    The task-creating routes require this header and reject a persona other than `plan`/`finish`.
    `CONTROL_TASK_ID` is exported only into a runner's shell (`fleet_dispatch.build_auth_lines`), so cron
    declares `host` while the same tool inside an implementer's session declares that implementer. Never
    hardcode the sentinel at a call site.

    `host` is NOT an unconditional fallback (a safe-off default may cover a switch, never an identity): inside
    a runner (`CONTROL_RUNNER_SESSION_ID` set — dispatched runners only, credential-independent) a missing task
    id means the id was LOST, so this declares nothing and the server 422s. Guards against accidental loss,
    not forgery. Not `COCKPIT_AGENT_BASE` or `JAAH_WORKTREE` as the runner test: operator shells carry those."""
    task_id_ = task_id().strip()
    if task_id_:
        return task_id_
    return "" if runner_session_id().strip() else "host"


# The installed standard's library root, exported into the launch env of every runner whose node is not at the
# package path (`fleet_dispatch.build_auth_lines`, valued `host_platform.paths().lib_root`; see
# `lib_root_relocated`). The Stop-hook gate and the pre-commit hook read it off Linux only; on Linux they keep the
# package's `/usr/lib/jaahd`. The control MCP server reads it on any OS to find the daemon tree.
LIB_ROOT_ENV = "JAAH_LIB_ROOT"

# The node-managed container engine's endpoint (`host_platform.paths().docker_host`), for a host whose
# engine is not the CLI's default socket. Exported into every runner's launch env
# (`fleet_dispatch.build_auth_lines`) and handed to every `docker` the daemon runs as the session user
# (`stack_ops._stack_env`, `reconcile`'s docker sensor), so `./dc` and gate commands run unchanged.
# Only the session user ever dials it: on macOS the socket behind it lives in that user's writable home,
# so no root or daemon-uid process may trust what answers there.
DOCKER_HOST_ENV = "DOCKER_HOST"


def docker_env() -> dict[str, str]:
    """`{DOCKER_HOST: <the engine>}` on macOS (Colima behind `/var/run/jaah/docker.sock`), else `{}`.

    Linux keeps the session user's own CLI context (its `docker_host` is empty anyway). Windows is not
    exported here yet: its engine (the `jaah-engine` WSL2 distro) wires its own endpoint."""
    import host_platform

    if not host_platform.is_macos():
        return {}
    return {DOCKER_HOST_ENV: host_platform.paths().docker_host}


# The package's library root as the shipped hook commands spell it (`infra/host/fleet-harness/settings.json`),
# with its trailing `/` so a bare `lib=/usr/lib/jaahd` default
# (the Stop hook's) is never touched.
PACKAGED_LIB_PREFIX = "/usr/lib/jaahd/"


def rehome_lib_paths(text: str, lib_root: str) -> str:
    """`text` with every `/usr/lib/jaahd/` re-rooted at `lib_root`, in forward slashes (`C:/Program
    Files/Jaah/current/`) — the spelling Git Bash, which runs a Windows node's hooks, reads in a quoted
    path. Used only where the installed standard does not live at the package path (`lib_root_relocated`)."""
    root = lib_root.replace("\\", "/").rstrip("/") + "/"
    return text.replace(PACKAGED_LIB_PREFIX, root)


def lib_root_relocated() -> bool:
    """True where this node's bundle is not at the package path: a Windows or macOS node, and a community-edition
    Linux node its installer staged under the user's data dir (`host_platform.linux._ce_paths`)."""
    import host_platform

    return host_platform.paths().lib_root.replace("\\", "/").rstrip("/") + "/" != PACKAGED_LIB_PREFIX


def python_cmd() -> str:
    """The interpreter a daemon-built command starts with: the node's own on Linux (`host_platform.paths().python`,
    the `jaah-python` package on a packaged node), never a PATH lookup — a bare `python3` there is the distro's,
    3.10 on Ubuntu 22.04, too old for the daemon tree. Bare `python3` on macOS and Windows, whose panes find the
    bundle's own interpreter first on PATH (`fleet_launch`)."""
    import host_platform

    return host_platform.paths().python if host_platform.is_linux() else "python3"


# `python3` as a COMMAND word: at the start of a line, of a JSON string value (`"command": "python3 …"`), or after
# a shell separator / keyword or a leading `NAME=value` assignment, and followed by whitespace — never inside a path
# (`/usr/bin/python3`) or a longer word.
_PYTHON_COMMAND = re.compile(
    r"(^|:\s*\"|[;&|({!`\n]|\b(?:then|do|else|exec|if|while|until)(?=\s)|\b[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]*(?=\s))(\s*)python3(?=\s)",
    re.M,
)


def rehome_python(text: str, python: str) -> str:
    """`text` with every `python3 ` at a command position rewritten to `python` (shell-quoted), for a shipped file
    whose commands spell bare `python3` (the fleet-harness template, `infra/host/fleet-harness/settings.json`). A no-op when
    `python` is `python3` itself. The shared source keeps the bare word; only the installed copy is rehomed, so a
    project's own `python3` and PATH are never touched."""
    if python == "python3":
        return text
    word = shlex.quote(python)
    return _PYTHON_COMMAND.sub(lambda m: f"{m.group(1)}{m.group(2)}{word}", text)
