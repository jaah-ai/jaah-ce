"""acp/launch.py — launch.json, the one file between the switch child that plans an ACP run and the host
that executes it (`jaah-acp-host --launch <launch.json>`).

Exactly `LAUNCH_FIELDS`, no more and no fewer: an unknown key is a writer/reader version skew, and a host
that guessed at it would run something nobody planned. A model key VALUE in `env` is refused on both
sides (`validate_env`): keys reach an engine through the daemon, never through a file on disk — `authenticate`
names how the host signs in, never with what. The same
holds for an MCP server's env and for `session_meta` keys, and any name shaped like a secret
(`SECRET_NAME`: `*_TOKEN`, `*_API_KEY`, `*_SECRET`, `*PASSWORD`) is refused unless `SECRET_NAME_ALLOWED`
lists it. The refusal names the variable and never the value, since the message lands in a pane and a log.

Stdlib only, no daemon imports (the host imports it before anything else can fail).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from acp import selfcheck

LAUNCH_FIELDS = ("task_id", "session_id", "provider", "auth_kind", "additional_dirs", "worktree",
                 "charter_path", "prompt_path", "runtime", "argv", "env", "session_meta", "mcp_servers",
                 "authenticate")
#: `authenticate`'s fields when set: the ACP auth method, the `_meta` key the key rides in, and the account (named in
#: a failure only — the daemon serves the key of the runner's leased account, `runtime.account_key`). Never a key.
AUTHENTICATE_FIELDS = ("method_id", "key_meta", "account")

_STR_FIELDS = ("task_id", "session_id", "provider", "auth_kind", "worktree", "charter_path", "prompt_path")
_REQUIRED_STR = ("task_id", "session_id", "provider", "worktree")
# The daemon's session-store rule (`acp/session_store.py`): the id becomes a filename there.
_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SECRET_NAME = re.compile(r"(_TOKEN|_API_KEY|_SECRET|PASSWORD)$", re.I)
# Secret-shaped names a launch may legitimately carry. Empty: the control MCP server's env
# (CONTROL_TASK_ID and kin) matches none of the shapes.
SECRET_NAME_ALLOWED: frozenset[str] = frozenset()
#: The one `session_meta` key that is the host's own (the native session a resume opens); the rest is the agent's.
#: Written by `fleet_dispatch.launch_runner` (a console hand-off's `resume_ref`), read by `acp/host.py`.
NATIVE_REF_KEY = "native_session_ref"
#: The only shape a resumed native session id may take: a canonical UUID (every hand-off engine's session id is
#: one). The id is reported by the session's own event stream, which the agent can write, and lands on a resume
#: argv: anything else (`--last`, `latest`, an index, a `-c` override) would reopen another conversation or
#: reconfigure the CLI. The CP checks the same shape (`apps/api/app/routes/console_runtime.py::_native_ref`).
NATIVE_REF = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


#: A registry install's text fields (`runtime.install`), beside its `pin` (and optional `agent`).
INSTALL_FIELDS = ("slug", "org_id", "dest")


class LaunchRefused(ValueError):
    """launch.json is not one this host will run. The message never carries an env value."""


@dataclass(frozen=True)
class LaunchSpec:
    task_id: str
    session_id: str
    provider: str
    auth_kind: str
    worktree: str
    additional_dirs: tuple[str, ...]
    charter_path: str
    prompt_path: str
    # `_runtime`'s dict, e.g. {"kind": "acp"}; read with .get by the host.
    runtime: dict
    argv: tuple[str, ...]
    env: dict
    session_meta: dict
    mcp_servers: tuple[dict, ...]
    # None, or `AUTHENTICATE_FIELDS`: the host signs in with the account's key before the session (`acp/host.py`).
    authenticate: dict | None = None


def validate_env(env: dict, *, where: str = "launch env", secret_names: bool = True) -> None:
    """Refuse an env carrying a non-empty model key (or, with `secret_names`, any non-empty secret-shaped
    name not in SECRET_NAME_ALLOWED). Names the variable, never the value."""
    for name in sorted(env, key=str):
        if not env[name]:
            continue
        if name in selfcheck.MODEL_KEY_VARS:
            raise LaunchRefused(
                f"{where} sets model key variable {name}; a key reaches the engine through the daemon, "
                "never through launch.json")
        if secret_names and isinstance(name, str) and SECRET_NAME.search(name) and name not in SECRET_NAME_ALLOWED:
            raise LaunchRefused(
                f"{where} sets secret-shaped variable {name}; a credential reaches the engine through the "
                "daemon, never through launch.json")


def mcp_env(server: dict) -> dict:
    """An MCP server entry's env as a mapping: ACP's `[{name, value}]` list, or a plain object."""
    env = server.get("env")
    if isinstance(env, dict):
        return env
    if isinstance(env, list):
        return {str(e.get("name")): e.get("value") for e in env if isinstance(e, dict)}
    return {}


def validate_secrets(env: dict, mcp_servers, session_meta: dict) -> None:
    """`validate_env` over the engine env, every MCP server's env, and the session_meta keys."""
    validate_env(env)
    for server in mcp_servers:
        if isinstance(server, dict):
            validate_env(mcp_env(server), where=f"mcp server {server.get('name')!r} env")
    _validate_nested(session_meta, "session_meta")


def _validate_nested(obj: object, where: str) -> None:
    """`validate_env` at every level of a nested object: a provider's `_meta` carries settings with an `env` of
    their own (an SDK adapter's `options.settings.env`), where a key would ride just as surely."""
    if isinstance(obj, dict):
        validate_env(obj, where=where)
        for key, value in obj.items():
            _validate_nested(value, f"{where}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _validate_nested(value, f"{where}[{index}]")


def _str_list(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise LaunchRefused(f"launch.json {field} must be a list of strings")
    return tuple(raw)


def _runtime(raw: object) -> dict:
    """The `runtime` field as a dict the host reads with `.get`: the switch writes the bare string `"acp"`
    (`acp_runtime.launch_document`); an object form (`{"kind": "acp", "idle_linger_s": ..., "nudge": ...}`)
    carries per-run overrides, the host's own lifecycle hook file (`lifecycle_hooks`, `acp/lifecycle.py`), and an SDK
    run's `run_dir`, plus a registry install's `install`, `profile`,
    `key_delivery` and `key_account`, or a bundle's `deps` (`agent_process.acp_runtime_extras`). Anything but ACP is
    refused: this host runs nothing else."""
    runtime = {"kind": raw} if isinstance(raw, str) else raw
    if not isinstance(runtime, dict) or runtime.get("kind", "acp") != "acp":
        raise LaunchRefused("launch.json runtime must be \"acp\" (or an object of kind acp)")
    for field in ("run_dir", "profile", "key_delivery", "key_account"):
        if not isinstance(runtime.get(field, ""), str):
            raise LaunchRefused(f"launch.json runtime.{field} must be a string")
    if not isinstance(runtime.get("lifecycle_hooks", {}), dict):
        raise LaunchRefused("launch.json runtime.lifecycle_hooks must be an object")
    install = runtime.get("install")
    if install is not None and not (
            isinstance(install, dict) and isinstance(install.get("pin"), dict)
            and all(isinstance(install.get(k), str) and install[k] for k in INSTALL_FIELDS)):
        raise LaunchRefused(f"launch.json runtime.install must carry a pin and {', '.join(INSTALL_FIELDS)}")
    deps = runtime.get("deps")
    if deps is not None and not (
            isinstance(deps, dict) and all(isinstance(deps.get(k), str) and deps[k] for k in ("dest", "name"))
            and isinstance(deps.get("command"), list) and deps["command"]
            and all(isinstance(word, str) for word in deps["command"])):
        raise LaunchRefused("launch.json runtime.deps must carry dest, name and a command (a list of strings)")
    return dict(runtime)


def _authenticate(raw: object) -> dict | None:
    """`authenticate`: null, or exactly `AUTHENTICATE_FIELDS`, each a non-empty string."""
    if raw is None:
        return None
    if not (isinstance(raw, dict) and set(raw) == set(AUTHENTICATE_FIELDS)
            and all(isinstance(raw[k], str) and raw[k] for k in AUTHENTICATE_FIELDS)):
        raise LaunchRefused(f"launch.json authenticate must be null or carry exactly {', '.join(AUTHENTICATE_FIELDS)}")
    return dict(raw)


def _from_dict(raw: object) -> LaunchSpec:
    if not isinstance(raw, dict):
        raise LaunchRefused("launch.json must be a JSON object")
    unknown = sorted(set(raw) - set(LAUNCH_FIELDS))
    missing = [f for f in LAUNCH_FIELDS if f not in raw]
    if unknown or missing:
        raise LaunchRefused(f"launch.json fields wrong: unknown={unknown} missing={missing}")
    for field in _STR_FIELDS:
        if not isinstance(raw[field], str):
            raise LaunchRefused(f"launch.json {field} must be a string")
    empty = [f for f in _REQUIRED_STR if not raw[f]]
    if empty:
        raise LaunchRefused(f"launch.json {', '.join(empty)} must not be empty")
    if not _SESSION_ID.match(raw["session_id"]):
        raise LaunchRefused("launch.json session_id must match [A-Za-z0-9._-]{1,128}")
    argv = _str_list(raw["argv"], "argv")
    runtime = _runtime(raw["runtime"])
    # A registry install names its engine only once the host has installed it (`runtime.install`).
    if (argv and not argv[0]) or (not argv and not runtime.get("install")):
        raise LaunchRefused("launch.json argv must name an executable")
    env = raw["env"]
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise LaunchRefused("launch.json env must map strings to strings")
    if not isinstance(raw["session_meta"], dict):
        raise LaunchRefused("launch.json session_meta must be an object")
    servers = raw["mcp_servers"]
    if not isinstance(servers, list) or not all(isinstance(s, dict) for s in servers):
        raise LaunchRefused("launch.json mcp_servers must be a list of objects")
    validate_secrets(env, servers, raw["session_meta"])
    return LaunchSpec(
        task_id=raw["task_id"], session_id=raw["session_id"], provider=raw["provider"],
        auth_kind=raw["auth_kind"], worktree=raw["worktree"],
        additional_dirs=_str_list(raw["additional_dirs"], "additional_dirs"),
        charter_path=raw["charter_path"], prompt_path=raw["prompt_path"], runtime=runtime,
        argv=argv, env=dict(env), session_meta=dict(raw["session_meta"]), mcp_servers=tuple(servers),
        authenticate=_authenticate(raw["authenticate"]),
    )


def load(path: str | os.PathLike) -> LaunchSpec:
    """Read and validate launch.json. Raises `LaunchRefused` (unreadable, malformed, or refused)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaunchRefused(f"launch.json unreadable at {path}: {type(exc).__name__}") from None
    return _from_dict(raw)


def to_dict(spec: LaunchSpec) -> dict:
    out = {field: getattr(spec, field) for field in LAUNCH_FIELDS}
    for field in ("additional_dirs", "argv", "mcp_servers"):
        out[field] = list(out[field])
    return out


def dump(spec: LaunchSpec, path: str | os.PathLike) -> None:
    """Write launch.json atomically (tmp + rename). Validates first: a refused spec is never written, so a
    key value never reaches disk. 0640: the host may run as another uid of the runner's group, nobody
    else reads a run's argv and env."""
    data = to_dict(spec)
    _from_dict(json.loads(json.dumps(data)))
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        os.fchmod(fd, 0o640)  # a stale tmp keeps its old mode through O_TRUNC
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
