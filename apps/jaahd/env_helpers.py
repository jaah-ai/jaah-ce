"""Shared env / host helpers for jaahd: dotenv parsing, the fleet config family, the
JAAH_HOME layout, the tmux-safe env, atomic file replacement and small subprocess-env helpers — one home,
no private copies.

STDLIB ONLY with NO import-time side effects: importing this never reads or loads a `.env`. That is
load-bearing for `_tmux_safe_env` (see its docstring). Its one sibling import, `environment`, is itself
a stdlib-only leaf.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path

import account_auth  # import-free leaf: the API-key account's config-dir spelling
import edition  # import-free leaf: the community edition keeps the node token out of the environment
import host_platform  # stdlib-only seam: the OS layout's daemon state root
from jaah_env import env_get  # stdlib-only leaf
from jaah_env import TRUTHY, env_flag_enabled, insecure_agent_enabled  # noqa: F401 — the one parse, re-exported for its node readers
import environment  # stdlib-only leaf: the ONE home of every outward surface's env var name and default

log = logging.getLogger("jaahd.env_helpers")

# The repo root (apps/jaahd/env_helpers.py → ../../..), used to locate `ctl.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ── dotenv parsing ──────────────────────────────────────────────────────────────

def _dequote(val: str) -> str:
    """Strip ONE layer of matching surrounding quotes from a dotenv value."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


def _dotenv_kv_quoted(raw: str) -> tuple[str, str, str] | None:
    """Parse ONE dotenv line into `(key, value, quote)`, or None when it declares no variable (blank, a
    comment, or no `=`). `quote` is the layer of surrounding quotes that was stripped — `"`, `'`, or
    `""` for a bare value. The single home for dotenv line rules; `load_fleet_env` needs `quote`
    because bash `source` takes a SINGLE-quoted value literally. Ignores `export ` and trims ` #` comments."""
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    key, _, val = line.partition("=")
    key = key.strip()
    val = val.split(" #", 1)[0].strip() if " #" in val else val.strip()
    quote = val[0] if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"') else ""
    return (key, _dequote(val), quote) if key else None


def _dotenv_kv(raw: str) -> tuple[str, str] | None:
    """`(key, value)` for one dotenv line, or None — `_dotenv_kv_quoted` without the quote."""
    parsed = _dotenv_kv_quoted(raw)
    return (parsed[0], parsed[1]) if parsed is not None else None


_DOTENV_VALUE_NOISE = re.compile(r"[\"' ]")


def dotenv_value(text: str, key: str) -> str:
    """What `key` resolves to in dotenv `text`, by the rule the compose env guard names the running
    stack with: the LAST `KEY=` line wins, an inline `#…` comment is cut, quotes and spaces are dropped;
    "" when absent. Not `_dotenv_kv` (bash-`source` rules, for `load_fleet_env`): the one reader behind
    every "which stack is this `.env`" question (`compose_project_name`, the pool-port checker's `env_port`,
    the worktree-agents snapshot's `env_key`) must agree with `./dc`, which exports the guard's reading."""
    prefix = re.compile(rf"^\s*{re.escape(key)}=")
    value = ""
    for line in text.splitlines():
        if prefix.match(line):
            value = _DOTENV_VALUE_NOISE.sub("", re.sub(r"\s*#.*", "", prefix.sub("", line, count=1)))
    return value


def compose_project_name(text: str) -> str:
    """A worktree `.env`'s COMPOSE_PROJECT_NAME (`dotenv_value`: the guard's rule) or "". The one rule: `reconcile.context.cpn_of` and `disk_reclaim.live_projects` both name a slot's compose
    project with it, so a reclaim never disagrees with the pool about which stack a volume belongs to."""
    return dotenv_value(text, "COMPOSE_PROJECT_NAME")


def _dotenv_lines(path: Path):
    """Yield (key, value) for each KEY=VALUE line in a dotenv file. A generator: errors surface to the
    iterating caller."""
    for raw in path.read_text().splitlines():
        kv = _dotenv_kv(raw)
        if kv is not None:
            yield kv


# ── the fleet config file family (`source` semantics) ────────────────────────────

# One bash `source X` / `. X` line, optionally behind a `[ -f X ] && ` guard. Anchored whole-line so a
# value merely containing `source` is never a directive.
_SOURCE_DIRECTIVE_RE = re.compile(
    r"""^(?:\[\s+-[a-zA-Z]\s+(?:"[^"]*"|'[^']*'|\S+)\s+\]\s*&&\s*)?   # optional `[ -f X ] &&` guard
          (?:source|\.)\s+(?P<path>"[^"]*"|'[^']*'|\S+)               # source X  |  . X
          \s*(?:\#.*)?$                                               # optional trailing comment
     """,
    re.VERBOSE,
)

# Fleet config lives at <jaah_home>/env unless FLEET_ENV overrides the path.
_FLEET_ENV_FILENAME = "env"


def _expand_shell_value(val: str) -> str:
    """`$VAR`/`${VAR}`/leading-`~` expansion for one config value, against the CURRENT os.environ —
    the part of bash `source` that a plain KEY=VALUE parser drops on the floor.

    A SUBSET of bash: `${VAR:-default}` and friends, `${#VAR}`, `$(cmd)`/backticks, `\\$VAR` and
    `$UNDEF/tail` all pass through differently. `load_fleet_env` warns on a surviving `$`."""
    return os.path.expanduser(os.path.expandvars(val))


def _source_directive(raw: str) -> str | None:
    """The (expanded) path a bash `source`/`.` line loads, or None when RAW is not such a line.

    Two shapes: bare `source X` / `. X`, and `[ -f X ] && source Y`. The guard is NOT evaluated (Y is
    loaded; a missing Y no-ops), and `source X || true` is skipped. A new form needs a case, not a guess."""
    match = _SOURCE_DIRECTIVE_RE.match(raw.strip())
    if match is None:
        return None
    return _expand_shell_value(_dequote(match.group("path"))) or None


def jaah_home() -> str:
    """The root directory under which THIS node keeps every jaah-managed thing on disk —
    accounts/auth, the fleet config file family, logs. Layout: `_LAYOUT`/`jaah_dir`.

    NO FALLBACK: guessing a root (or deriving it from `Path.home()`, the OS user's home) would silently
    point a process at the wrong state. Durable source is `/etc/jaah/env`. Resolution lives in
    `environment.pool_root()`."""
    return environment.pool_root()


def fleet_env_path() -> Path:
    """The fleet config file to load: `$FLEET_ENV` when set, else `<jaah_home>/env`."""
    return Path(os.environ.get("FLEET_ENV") or f"{jaah_home()}/{_FLEET_ENV_FILENAME}")


# ── The console bearer token is a NODE fact ─────────────────────────────────────────────────────────
# `NODE_TOKEN` is a NODE fact, spelled only in:
#   * `$JAAH_HOME/env.local` — a `*_NODE_AGENT_TOKEN` variable (its pre-rename name: every hub host's `env.local`
#     spells it so), mapped by the host's `fleet.env` (CP host).
#   * `/etc/jaah/node.env` — `NODE_ID`, `NODE_NAME`, `NODE_TOKEN`; written by
#     `jaah auth login <uuid>`, root-owned 0640, loaded by the unit as an `EnvironmentFile`.
# NOT a project worktree's `.env` (worktrees seed `.env` from each other, so one leak would be every
# checkout) — except this repo's own, whose `apps/api` validates `NODE_TOKEN` in its tests.


def node_env_path() -> Path:
    """The node-level identity/token file: `$JAAH_NODE_ENV_FILE` when set, else `/etc/jaah/node.env`.
    Resolved by `environment.node_env_path()`; wrapped in `Path`."""
    return Path(environment.node_env_path())


def environ_node_token() -> str:
    """`NODE_TOKEN` as the daemon's own environment carries it, read at call time — or, once the daemon removed it
    from its environment at start (`scrub_node_token_from_environ`), from `node.env` (`resolve_node_token`)."""
    return resolve_node_token()


def scrub_node_token_from_environ() -> bool:
    """Drop `NODE_TOKEN` from this process's environment, so no child inherits it.

    Every tmux client the daemon runs sends its whole environment to the fleet tmux server, and that server is
    the session user's (`jaah`, on macOS `_jaah`; on a cloud Linux node it owns the socket and its `tmux-<uid>`
    directory and can put a listener in its place): a token left in the environment reaches it on the first
    call. The daemon's own readers fall back to `node.env`, which only it can read (`environ_node_token`,
    `resolve_node_token`; `control_plane_api` captured the value at import). On Linux off the community
    edition the token is dropped only when `node.env`
    carries one, so a dev host whose token comes from the fleet env alone keeps it. Returns whether a value
    was removed."""
    if not (host_platform.is_windows() or edition.IS_CE or _node_env_token()):
        return False
    return os.environ.pop("NODE_TOKEN", None) is not None


def _node_env_token() -> str:
    """`NODE_TOKEN` from `node_env_path()`, or "" when the file is absent, unreadable or names none."""
    try:
        for key, val in _dotenv_lines(node_env_path()):
            if key == "NODE_TOKEN" and val.strip():
                return val.strip()
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        # The designed answer for a non-service user (0640 root:jaahd), so not a warning: a packaged
        # install printed it to the operator as an error. Return "", never raise.
        log.debug("cannot read the node env file %s: %s", node_env_path(), exc)
    except OSError as exc:
        log.warning("cannot read the node env file %s: %s", node_env_path(), exc)
    return ""


def resolve_node_token() -> str:
    """This NODE's console bearer token, or "" when the node has none provisioned.

    Arms, in order: (1) `os.environ["NODE_TOKEN"]` — the unit's EnvironmentFile, and the fleet-env
    family via `load_fleet_env` (re-reading that file raw would return the unexpanded
    `env.local` variable); (2) `node_env_path()`, readable only by the daemon (0640
    root:jaahd) — a `jaah`-user launcher gets `PermissionError`, correctly: it needs no token.

    "" is a real answer (a dev checkout); `_agent_headers` then omits the header rather than 401."""
    return os.environ.get("NODE_TOKEN", "").strip() or _node_env_token()


# ── The JAAH_HOME directory layout ────────────────────────────────────────────────────────────────
# The one table of reserved `<jaah_home>/<kind>/<subject>` kinds, so nothing else in the home collides
# with runtime state. Clones live under `repos` (keyed by `projects.id`, so a rename never moves one). Each
# row carries its description as data because the fleet doctor prints it (a comment would be copied
# and drift).
_LAYOUT: dict[str, tuple[str, str]] = {
    "run": ("run", "ephemeral: locks, IPC, pids — safe to delete when nothing is running"),
    "log": ("log", "append-only text a human reads: log/cron, log/daemon, log/agent"),
    "state": ("state", "durable stamps a sweep reads back: state/sweep, state/stuck, state/attn, "
                       "state/node"),
    "cache": ("cache", "derivable, safe to delete: cache/cookie, cache/gh-pr-guard"),
    "data": ("data", "the only user-data kind: data/backup, data/snapshots"),
    "omc": ("omc", "OMC working memory"),
    "trace": ("trace", "runner traces"),
    "auth": ("auth", "credential material: a per-account config dir"),
    "creds": ("creds", "credential material: node-local credential state"),
    "repos": ("repos", "project clones and pool worktrees: repos/<project id>/{main,N}"),
}


# WHERE THE HOME DIRNAME IS SPELLED LITERALLY (`~/.jaah`):
#   bootstrap: the host installer's `JAAH_HOME_DIRNAME` (`fleet.env` exports JAAH_HOME itself), and
#     apps/api/app/node_bootstrap.py `PROVISIONED_JAAH_HOME` for a CP-created node.
#   soft defaults, for contexts that run with no JAAH_HOME and must not raise:
#     tools/jaah_paths.py (cron) and its shell twin (the gate-admission lib), apps/jaahd/environment.py
#     `hook_log_dir` (runner hooks), dc, the node reprovision script (first boot).
#   also: the host `fleet.env`'s template line (the installer rewrites it), apps/jaahd/host_platform/linux.py
#     `home_root`, and vnode homes (the CP's vnode `jaah_home_for`, apps/jaahd/vnodehelper/layout.py).
# Everything else resolves through `jaah_home()`/`jaah_dir()`.
def jaah_dir(kind: str, *parts: str) -> Path:
    """`<jaah_home>/<kind>/<parts...>` for one of the reserved `_LAYOUT` kinds. Raises on an unknown
    kind (the reserved set stays closed). Does NOT create the directory: read-only callers stay readers."""
    if kind not in _LAYOUT:
        raise ValueError(
            f"unknown JAAH_HOME layout kind {kind!r} - add it to env_helpers._LAYOUT "
            f"(known: {', '.join(sorted(_LAYOUT))})"
        )
    return Path(jaah_home()).joinpath(_LAYOUT[kind][0], *parts)



def fleet_locks_dir() -> Path:
    """Where the daemon keeps its flock files: `<jaah_home>/run/locks` — except on Windows, where the
    runner account holds Modify on the whole home and could redirect the daemon's create through a
    junction (which needs no privilege there), so they live in the daemon-only `state\\locks`. Lazy:
    importing needs no `JAAH_HOME`. Does NOT create the directory."""
    if host_platform.is_windows():
        return Path(host_platform.paths().state_dir, "locks")
    return jaah_dir("run", "locks")

def log_dir(*parts: str) -> Path:
    """`$FLEET_LOG_DIR` when set (tests, a non-default host layout), else `<jaah_home>/log`.

    `or`, not a dict default: like bash's `${FLEET_LOG_DIR:-...}`, set-but-EMPTY means unset."""
    override = os.environ.get("FLEET_LOG_DIR") or ""
    root = Path(override) if override.strip() else jaah_dir("log")
    return root.joinpath(*parts)


# The packaged answer for `daemon_state_dir`, named because the fleet doctor's `--reset-dev` refuses the
# daemon's trees BY NAME, and the accessor's `$HOME` fallback would name the invoking user's home instead.
DAEMON_STATE_DIR_DEFAULT = host_platform.paths().state_dir   # `/var/lib/jaahd` on Linux


def daemon_state_dir(*parts: str) -> Path:
    """The DAEMON'S OWN state root — `$HOME`, pinned by the packaging to `/var/lib/jaahd`
    `0750 jaahd:jaah`. THE ONE HOME of that answer; `main.daemon_state_dir` delegates here.

    A trust boundary: `jaah_dir`/`state_dir` sit in the SESSION user's tree, which every runner can
    write, so anything a runner must not plant or redirect (e.g. the direct-console TLS private key)
    belongs here. `$HOME` first so a dev checkout lands somewhere real.
    """
    return Path(os.environ.get("HOME") or DAEMON_STATE_DIR_DEFAULT).joinpath(*parts)


def state_dir(*parts: str) -> Path:
    """`$FLEET_STATE_DIR` when set, else `<jaah_home>/state` — durable sweep stamps, NOT logs.

    Split from `log_dir` so relocating logs never re-arms idempotency stamps. Empty means unset."""
    override = os.environ.get("FLEET_STATE_DIR") or ""
    root = Path(override) if override.strip() else jaah_dir("state")
    return root.joinpath(*parts)


def resolve_clone_root(project_id: int | str | None) -> str:
    """THIS node's main-clone path for a project, computed the same way `app/node_clone.py::resolve_clone`
    computes it server-side: `<jaah_home>/repos/<project id>/main`. "" when the id is missing or not an
    integer, or JAAH_HOME is unset — no implicit default, never a raise."""
    if isinstance(project_id, bool):
        return ""
    try:
        pid = int(project_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if pid < 0:
        return ""
    try:
        return str(jaah_dir("repos", str(pid), "main"))
    except RuntimeError as exc:
        log.debug("resolve_clone_root(%r): %s", project_id, exc)
        return ""


def _writable_by_others(info: os.stat_result) -> bool:
    """Whether a user other than the file's owner can write it: world-writable, or group-writable by a group with
    any member but the owner. A dev host's private user group writes nothing the owner could not (umask 0002); a
    fleet node's `jaah` group holds the session user, and the daemon's own files in `$JAAH_HOME` are
    `jaahd:jaah 0664`."""
    import grp
    import pwd
    if info.st_mode & stat.S_IWOTH:
        return True
    if not info.st_mode & stat.S_IWGRP:
        return False
    try:
        group = grp.getgrgid(info.st_gid)
        owner = pwd.getpwuid(info.st_uid)
    except KeyError:
        return True
    return bool(set(group.gr_mem) - {owner.pw_name}) or info.st_gid != owner.pw_gid


def _is_node_token_env_name(key: str) -> bool:
    """Whether the fleet env family spells the node token under `key` (the host's `fleet.env`: `NODE_TOKEN`, mapped
    from `env.local`'s `*_NODE_AGENT_TOKEN`). Never loaded where `node.env` holds the token: a launcher run as the
    daemon would hand it to its tmux clients."""
    return key == "NODE_TOKEN" or key.endswith("_NODE_AGENT_TOKEN")


def load_fleet_env(
    path: Path | None = None, *, _seen: set[str] | None = None, _set_here: set[str] | None = None,
) -> None:
    """Load the FLEET CONFIG file family (`$JAAH_HOME/env` plus whatever it `source`s) into os.environ,
    reproducing the SUBSET of bash `source` it uses: `$VAR`/`${VAR}`/`~` expansion of bare and
    double-quoted values (single-quoted stay literal) and `_source_directive`'s two shapes. PATH defaults
    to `fleet_env_path()`; a missing/garbled file is a no-op.

    Not `_dotenv_lines`, which reads a WORKTREE `.env` literally. This family is shell: values reference
    other vars, and it `source`s `env.local` — skipping either leaves literal `$HOME/...` paths or unset
    vars, which can drop a session onto the default credential dir, bypassing the account pool.

    Precedence: (1) the AMBIENT env at entry beats the whole family (launchers get `_tmux_safe_env`'s
    narrow env, so the file fills gaps; unlike bash `export`); (2) within the family later wins, so
    `env.local` overrides `env`. Hence `_set_here`, not "present in os.environ now"."""
    path = fleet_env_path() if path is None else Path(path)
    seen = set() if _seen is None else _seen
    # Keys THIS pass wrote (overridable by a later file); others in os.environ are ambient.
    set_here = set() if _set_here is None else _set_here
    # Cycle guard: `env.local` sourcing `env` back would otherwise recurse forever.
    real = os.path.realpath(path)
    if real in seen:
        return
    seen.add(real)
    try:
        with open(path, encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            owner, mode = info.st_uid, stat.S_IMODE(info.st_mode)
            if not host_platform.is_windows() and (owner not in (0, os.geteuid()) or _writable_by_others(info)):
                # Never take this process's environment from a file another uid wrote: `$JAAH_HOME` is the session
                # user's to write, and a `JAAHD_CTL`/`LD_PRELOAD`/`PYTHONPATH` there would run a session's code as
                # the daemon. Its legitimate writers are root (packaging) and the uid
                # running the host installer, the same uid as the daemon on a host that uses it. Checked on
                # the open descriptor, so a swap between check and read is refused too. `env` may point at any file,
                # so one another user can write is refused like a foreign one (`_writable_by_others`).
                log.warning("ignoring fleet env at %s: owned by uid %s, mode %o — it must be this process's or "
                            "root's, and writable by no other user", path, owner, mode)
                return
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return
    except Exception as exc:  # never let a malformed fleet env raise into a launcher
        log.warning("ignoring unreadable fleet env at %s: %s", path, exc)
        return
    for raw in lines:
        sourced = _source_directive(raw)
        if sourced is not None:
            load_fleet_env(Path(sourced), _seen=seen, _set_here=set_here)
            continue
        parsed = _dotenv_kv_quoted(raw)
        if parsed is None:
            continue
        key, val, quote = parsed
        if _is_node_token_env_name(key) and _node_env_token():
            continue                      # `node.env` holds the token: it stays out of every environment
        if key in os.environ and key not in set_here:
            continue                      # ambient (pre-existing at entry) — the daemon's env wins
        # bash `source`: a bare or double-quoted value expands, a SINGLE-quoted one is literal.
        expanded = val if quote == "'" else _expand_shell_value(val)
        if quote != "'" and ("$" in expanded or "${" in expanded):
            # An unsupported substitution form; loud because it otherwise surfaces much later as a
            # FileNotFoundError.
            log.warning(
                "fleet env %s: %s still contains an unexpanded '$' after expansion (%r) — unsupported "
                "shell substitution form; only $VAR, ${VAR} and a leading ~ are expanded", path, key, expanded,
            )
        os.environ[key] = expanded
        set_here.add(key)


def fleet_ctl_path() -> str:
    """Path to jaahd's `ctl.py`, which the interactive launchers shell out to. `JAAHD_CTL`
    overrides it (the debug/test seam)."""
    return env_get("JAAHD_CTL") or str(_REPO_ROOT / "apps" / "jaahd" / "ctl.py")


def roster_config_dir(rec: object) -> str:
    """One roster row's config dir on THIS node: the CP's `config_dir` for an OAuth account, the node-derived
    `account_auth.api_key_config_dir` for an API-key one (served `config_dir=""`), `""` otherwise.

    The one reading of a roster row as a directory, so the probe set, the first-sight converge and the
    provisioning sweep all see the API-key homes a runner launches into."""
    if not isinstance(rec, dict):
        return ""
    config_dir = rec.get("config_dir")
    if config_dir:
        return str(config_dir)
    if rec.get("auth_kind") == account_auth.AUTH_KIND_API_KEY:
        return account_auth.api_key_config_dir(environment.pool_root_or_empty(), rec.get("slug"))
    return ""


def registry_norm(d: str) -> str:
    """Normalize a config_dir to the registry/ledger join key, byte-faithful to bash's
    `${1/#\\~/$HOME}` then `${d%/}`: a LEADING `~` becomes $HOME and ONE trailing slash is stripped. Not
    `normpath(expanduser(...))` (`_norm_dir`), which would diverge on `.`/`..`/`//`, `"/"` and `~user`."""
    home = os.environ.get("HOME", "~")
    if d.startswith("~"):
        d = home + d[1:]
    return d[:-1] if d.endswith("/") else d


# ── Agents-probe invalidation (cross-module, process-global) ────────────────────
# Mutating commands mark an account's config_dir so the next tick refreshes `reconcile.accounts_probe.AgentsProbeCache`
# immediately instead of waiting out its 30s/3600s floor. A process global here because the mark sites
# (`fleet_dispatch`, `commands`, `main`) share only this stdlib-only module; keyed by `registry_norm`.
# The lock is load-bearing: `dispatch_one` runs in a `to_thread` worker while the tick drains on the loop.
_agents_dirty_dirs: set[str] = set()
_agents_dirty_lock = threading.Lock()


def mark_agents_dirty(config_dir: object) -> None:
    """Mark one account's `claude agents` cache entry as due for refresh on the next tick.

    Does NOT probe (a CLI cold-start would add ~0.5s to the command). A falsy/non-str `config_dir` is a
    no-op, never "invalidate everything"."""
    if not isinstance(config_dir, str) or not config_dir:
        return
    with _agents_dirty_lock:
        _agents_dirty_dirs.add(registry_norm(config_dir))


def take_agents_dirty() -> set[str]:
    """Drain the marked config_dirs — the caller commits to probing what it takes."""
    with _agents_dirty_lock:
        taken = set(_agents_dirty_dirs)
        _agents_dirty_dirs.clear()
    return taken


# ── tmux-safe subprocess env ────────────────────────────────────────────────────

# The host vars a login shell needs. Narrow on purpose — see `_tmux_safe_env`.
_TMUX_HOST_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "USER", "LANG", "LC_ALL")

# Windows-only additions: what a Windows process needs to START at all (without `SystemRoot` DLLs fail to
# load, and Git's MSYS runtime — AF_UNIX over Winsock, which tmux's socket is — cannot initialise), where
# its per-user and temp folders are, and how MSYS / Claude Code find Git. None is a secret.
_TMUX_WINDOWS_ENV_KEYS: tuple[str, ...] = (
    "SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
    "USERNAME", "USERDOMAIN", "COMPUTERNAME", "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles",
    "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "MSYS", "MSYSTEM", "MSYS2_PATH_TYPE", "JAAH_GIT_ROOT",
    "CLAUDE_CODE_GIT_BASH_PATH",
)

# Which directory the tmux sockets live in (`session_user.tmux_tmpdir`): stripping `TMUX_TMPDIR` would
# silently relocate every socket path. Servers are still found via mandatory `-S` from one seam
# (`session_user.socket_args`/`tmux_cmd`/`manage_sockets`), since the default label resolves per-EUID.
# The run root and vnode flag travel too, else a vnode child resolves the HOST's tree.
_TMUX_SOCKET_ENV_KEYS: tuple[str, ...] = (
    environment.TMUX_TMPDIR_ENV, environment.RUN_ROOT_ENV, environment.VNODE_ENV)

# The one spelling of this env var name; `fleet_launch.tmux_session()` imports it.
TMUX_SESSION_ENV_VAR = "FLEET_TMUX_SESSION"

# Legal tmux session names: no `:`/`.` (tmux separators), no leading `-` (a flag). Imported by
# `console._validate_session_name`; mirrored by the CP's `console_direct.validate_session_name`.
SESSION_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

class NoNodeIdentity(RuntimeError):
    """This node has no usable identity, so it has no tmux session name — raised instead of guessing one.

    No shared default: a fallback name exists on no node. An unresolvable identity is a provisioning
    state (`jaah auth login <uuid>` has not run)."""


def default_tmux_session(env: dict | None = None) -> str:
    """This NODE's own tmux session name: its `NODE_NAME`. Raises `NoNodeIdentity` when there is none.

    One server per node, so the session name must distinguish nodes: the human `NODE_NAME`, not the uuid.
    ASSIGNED, never derived (no hostname fallback — `main.resolve_node_id`'s rule).

    The `NODE_ID` arm is TRANSITIONAL for nodes whose `node.env` predates `NODE_NAME`; DELETE it once
    every node carries one (`jaah auth status`). An illegal name is refused, not sanitized (the
    mangling would need three identical copies). Reads env: the unit loads `node.env` and
    `_tmux_safe_env()` forwards both keys."""
    source = env if env is not None else os.environ
    name = source.get("NODE_NAME", "").strip()
    if SESSION_NAME_RE.match(name):
        return name
    node_id = source.get("NODE_ID", "").strip()
    if SESSION_NAME_RE.match(node_id):
        return node_id
    raise NoNodeIdentity(
        f"this node has no usable NODE_NAME ({name!r}) or NODE_ID ({node_id!r}), so it has no tmux "
        f"session name — run `jaah auth login <uuid>`"
    )


# Needed by the LAUNCHER process (resolve-once-then-pass-down). As a window key it only ever carries the
# session `vis_window` RESOLVED, which beats an ambient `NODE_ID` (the compose env guard exports a
# worktree's own).
_TMUX_LAUNCHER_ENV_KEYS: tuple[str, ...] = (TMUX_SESSION_ENV_VAR,)

# Needed INSIDE the launched window; a long-lived server's frozen env lacks them, so `tmux new-window -e`
# names them (`tmux_window_env_args`). PATH is deliberately NOT here: the daemon's PATH must never name
# `~jaah/.local/bin` (`session_user.delegated_path()`), so it would not contain `claude` anyway; a
# pane's PATH is set by `fleet_dispatch._launch_no_model` and `jaah-runner@.service`.
TMUX_WINDOW_ENV_KEYS: tuple[str, ...] = (
    environment.CP_BASE_URL_ENV, "NODE_ID", "NODE_NAME", "IS_SANDBOX", environment.POOL_ROOT_ENV,
    TMUX_SESSION_ENV_VAR,
)


def tmux_window_env_args(env: dict) -> list[str]:
    """`["-e", "K=V", ...]` for the `TMUX_WINDOW_ENV_KEYS` present in ENV — the flags
    `tmux new-window`/`new-session` need so a window's environment does not depend on the SERVER's.

    A tmux server freezes its first client's env and hands it to every window it ever hosts (for weeks),
    so a var added since never reaches new windows. `-e` scopes each value to the window being created
    (tmux 3.4+)."""
    args: list[str] = []
    for key in TMUX_WINDOW_ENV_KEYS:
        value = env_get(key, None, env)
        if value is not None:
            args += ["-e", f"{key}={value}"]
    return args


def _tmux_safe_env() -> dict:
    """The env every tmux-invoking subprocess in this daemon must use — this function is the single
    home for that guard. ANY tmux client command silently auto-starts the server when none is running,
    and the server freezes that invocation's env into every window until restarted.

    Unlike the former host-agent's variant: this daemon never dotenv-loads `.env` into its own process
    (its env is the unit's /etc/jaah EnvironmentFiles), so there is no ambient-secret poisoning to
    strip — this just narrows to what a login shell needs, so NODE_TOKEN/GITHUB_TOKEN never freeze into
    windows. Porting the `_HOST_ENV`-minus-dotenv-keys variant would require loading `.env` at import.

    The non-secret additions, each required: `TMUX_SESSION_ENV_VAR` (else spawns retarget); the CP base
    URL (else a cloud node's launchers dial localhost; propagated, never derived); `NODE_ID` (else slot
    lookups hit another node's row) and `NODE_NAME` (`default_tmux_session`); `JAAH_HOME` (else
    `load_fleet_env()` raises); `IS_SANDBOX`, DERIVED from `os.geteuid() == 0` via `setdefault` (Claude
    Code refuses `--dangerously-skip-permissions` as root, and cloud nodes run as root; an explicit value
    still wins)."""
    keep = (*_TMUX_HOST_ENV_KEYS, *_TMUX_LAUNCHER_ENV_KEYS, *TMUX_WINDOW_ENV_KEYS,
            *_TMUX_SOCKET_ENV_KEYS)
    windows = host_platform.is_windows()
    if windows:
        keep = (*keep, *_TMUX_WINDOWS_ENV_KEYS)
    env: dict = {}
    for k in keep:
        value = env_get(k)
        if value is not None:
            env[k] = value
    # /usr/local/bin FIRST: legacy AL2023 nodes keep the python3.12 shim there (tools need 3.11+).
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # systemd gives the daemon NO HOME; without one PATH gets `/.local/bin` and `set -u` scripts die. This is
    # the daemon's own home, for tmux clients and directly-spawned launchers — NOT a window's (panes get the
    # session user's server env; use `-e` or `session_user.session_home()`).
    env.setdefault("HOME", os.path.expanduser("~"))
    env.setdefault("TERM", "xterm-256color")
    # FORCED: tmux takes `default-shell` from $SHELL, else the passwd shell — `jaah`'s is `nologin`, which
    # exits at once and collapses the new pane, window, session and server while tmux reports success.
    # Hardcoded because neither the nologin account nor root is guaranteed to supply bash.
    env["SHELL"] = "/bin/bash"
    # No root on Windows (and no `os.geteuid`): the IS_SANDBOX derivation is a posix one.
    if not windows and os.geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")
    return env


# Socket widening is homed in `session_user` (`share_own_socket`, `restrict_own_socket`, `socket_dir_for`):
# tmux creates the dir and socket 0700 regardless of umask, and `tmux-<euid>` is tmux's own component.


# Home-relative roots walked — NOT the whole home (pool worktrees run to gigabytes). The daemon's own files
# are group-readable already (`UMask=0002` + setgid dirs); these hold the app-written private-mode
# exceptions. The account root comes from `jaah_home()`, not a second spelling.
_SHARE_HOME_ROOTS = (".claude", ".local/state")

# Skipped because widening is FUTILE, not because it is secret: `.credentials.json` is rewritten 0600 on
# every token refresh and `claude` start. Secret-bearing files written once are widened like the rest.
_SHARE_HOME_SELF_RESETTING = {".credentials.json"}

# The ONLY secrecy exclusion: `remote-settings.json` carries a telemetry `Authorization=Bearer …`, and
# unlike `.credentials.json` a widening here would stick. (`remote-settings-consent.json` is widened.)
_SHARE_HOME_SECRET = {"remote-settings.json"}


# Floor so the 5s idle tick does not re-walk 12 times a minute.
_SHARE_HOME_MIN_INTERVAL_S = 60.0
_share_home_last_run = 0.0


def share_home_readable(
    home: str | None = None, force: bool = False, *, account_tree: bool = True
) -> int:
    """Keep the service user's own files readable to the `jaah` group (D9 of spec
    jaah-package-distribution). Returns how many paths it widened.

    Code, not config: apps write private modes regardless of umask, and a default ACL does not help (a
    0600 request masks the entry to nothing), so only re-applying on the idle tick converges. Adds group
    READ (and X on dirs) only. `_SHARE_HOME_SELF_RESETTING` names are skipped; `_SHARE_HOME_SECRET` names
    are NARROWED back to owner-only.

    RUN IT AS THE TREE'S OWNER or it does nothing (chmod EPERMs, logged only at DEBUG). The account
    tree's converge therefore rides `fleet_tmux_server.converge_forever` as `jaah`;
    `account_tree=False` is how a non-owner caller skips that expensive walk. Best-effort throughout.

    0 on Windows, without walking: `os.stat` there synthesises mode bits (group-read always "set") and
    `os.chmod` only toggles read-only, so a pass would widen nothing and `_narrow` would "narrow" the same
    file every minute. Access there is the installer's inherited ACLs."""
    global _share_home_last_run
    if host_platform.is_windows():
        return 0
    base = (home or os.path.expanduser("~")).strip().rstrip("/")
    if not base or not os.path.isdir(base):
        return 0
    now = time.monotonic()
    if not force and (now - _share_home_last_run) < _SHARE_HOME_MIN_INTERVAL_S:
        return 0
    _share_home_last_run = now

    roots = [os.path.join(base, rel) for rel in _SHARE_HOME_ROOTS]
    if not account_tree:
        return _report(_widen(roots), base)
    try:
        # No JAAH_HOME means no account tree to converge, not an error.
        roots.insert(0, os.path.join(jaah_home(), "auth"))
    except Exception as exc:  # noqa: BLE001 — a home-less node still gets the rest of the pass
        log.debug("share_home_readable: no resolvable account tree: %s", exc)

    return _report(_widen(roots), base)


def _report(widened: int, base: str) -> int:
    """Log the pass's own count and return it — BOTH exits go through here, deliberately.

    Both exits go through here so neither is silent."""
    if widened:
        log.info("share_home_readable: widened %d path(s) under %s to group-readable", widened, base)
    return widened


def _widen(roots: "list[str]") -> int:
    """Walk each root, widening what may be widened and NARROWING what must not be. Returns the count.

    Counts WIDENED paths only; a narrowing is a correction, not a share.
    """
    widened = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
            # Never a link, and never a MOUNT: the accounts NodeLink sits at `auth/.shared` and is the share's,
            # not this node's home to re-mode.
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
                           and not os.path.ismount(os.path.join(dirpath, d))]
            for name in [*dirnames, *filenames]:
                path = os.path.join(dirpath, name)
                if name in _SHARE_HOME_SECRET:
                    _narrow(path)
                    continue
                if name in _SHARE_HOME_SELF_RESETTING:
                    continue
                try:
                    if os.path.islink(path):
                        continue
                    mode = os.stat(path).st_mode
                    # `X` in `g+rX`: execute for directories only.
                    want = stat.S_IRGRP | (stat.S_IXGRP if stat.S_ISDIR(mode) else 0)
                    if (stat.S_IMODE(mode) & want) == want:
                        continue
                    os.chmod(path, stat.S_IMODE(mode) | want)
                    widened += 1
                except OSError as exc:
                    log.debug("share_home_readable: could not widen %s: %s", path, exc)
    return widened


def _narrow(path: str) -> None:
    """Take group and other bits OFF a `_SHARE_HOME_SECRET` file. Best-effort, like everything here.

    Skipping is not enough: earlier passes already widened existing copies, and only narrowing
    remediates them.

    Owner bits are untouched: the writer is the account's own `claude`, which owns the file."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return
        mode = stat.S_IMODE(os.stat(path).st_mode)
        private = mode & ~(stat.S_IRWXG | stat.S_IRWXO)
        if private != mode:
            os.chmod(path, private)
            log.info("share_home_readable: narrowed %s back to owner-only", path)
    except OSError as exc:
        log.debug("share_home_readable: could not narrow %s: %s", path, exc)


# ── worktree records ─────────────────────────────────────────────────────────────
# No slot reader here: `control_plane_api` imports this module, so each consumer calls
# `control_plane_api.agent_api_pool_worktrees`/`agent_api_worktree` itself, keeping its own failure posture.


# ── misc ─────────────────────────────────────────────────────────────────────────

def now_iso(ts: float | None = None) -> str:
    """UTC as `%FT%TZ` (second precision, `Z`) — the one home of that stamp: now, or epoch `ts` when given."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def atomic_write_text(path: Path, content: str | bytes, *, mode: int, fsync: bool = False,
                      suffix: str = ".tmp") -> None:
    """Replace `path` with `content` (text, or bytes written as-is) atomically at `mode`: a `mkstemp` file
    `.<name>.*<suffix>` in the same directory (same filesystem, so `os.replace` is a rename), chmodded
    BEFORE the rename so the path never exists wider.
    A concurrent reader sees the old file or the new one, never half of either; on failure the temp file
    is removed and the original is untouched. `os.replace` also hands the target the temp file's owner,
    and replaces a symlink rather than writing through it."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=suffix)
    binary = isinstance(content, bytes)
    try:
        with os.fdopen(fd, "wb" if binary else "w", encoding=None if binary else "utf-8") as fh:
            fh.write(content)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: object, *, mode: int, indent: int | None = None,
                      sort_keys: bool = False, fsync: bool = False, suffix: str = ".tmp") -> None:
    """`atomic_write_text` of `data` as JSON — the one home of "serialise, then replace atomically". An
    indented document ends with a newline, as a file a person opens should."""
    text = json.dumps(data, indent=indent, sort_keys=sort_keys)
    atomic_write_text(path, text + "\n" if indent is not None else text, mode=mode, fsync=fsync, suffix=suffix)


def _norm_dir(p: object) -> str:
    """Normalized absolute form of a config-dir path, for matching a worktree's `account` against a
    registered account's config_dir (both are written from the same source, but normalize defensively)."""
    return os.path.normpath(os.path.expanduser(str(p))) if p else ""

