"""omx_layer.py — converge a Codex account onto the oh-my-codex (OMX) layer, the Codex counterpart of the
OMC plugin `plugin_repair.py` installs into every Claude account.

What one converge does, in order (`converge_account`):
  1. `omx setup --scope user` with `CODEX_HOME=<account dir>` — writes prompts/skills/agents, `AGENTS.md`,
     `.omx/`, MERGES `hooks.json` (keeps foreign entries) and APPENDS to `config.toml`. Measured
     unattended (stdin closed) against omx 0.21.6 / codex-cli 0.152.1: rc 0, ~1s on rerun.
  2. `.omx-config.json` — the notification policy, the SAME shape as OMC's `.omc-config.json`; both are
     written by `notification_policy.write_notification_policy`, the one writer of that shape.
  3. `codex mcp add` for the two companion servers that mirror the Claude MCP plugins. Idempotent
     (measured: a rerun overwrites, rc 0).

The npm refresh (`refresh_packages`: omx + codex, each at its `runner_cli_pins` pin) is NOT part of a converge — it is
once per run, the caller's. It refuses to run as root: root-owned files under the runner home are the
escalation the package's postinst forbids. It is `runner_cli_pins.py install`, which verifies each tarball
against its pin, installs its locked dependency tree and holds the prefix lock across the install.

`hooks.json` is 0600 and unreadable to the daemon, so nothing here reads it; `merge_hooks` is the pure
merge the adapter (`agents/codex.py`) applies when it rewrites the file as the session user: the fleet's
entries first, OMX's own (their command names the package) preserved verbatim, the fleet's stale ones
dropped.

`run(argv, *, env=None, timeout=None, cwd=None) -> {"exit_code", "stdout", "stderr"}` everywhere (`cwd`
None = the caller's own); a missing binary or a timeout is exit_code -1, never an exception. `env` is passed FULL (merged over os.environ here) and
is never logged or returned. Nothing here raises. Stdlib only (runs from the packaged tree).
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

import file_io  # stdlib-only leaf: the one no-follow, non-blocking regular-file open
import runner_cli_pins
from notification_policy import write_notification_policy

OMX_PACKAGE = "oh-my-codex"
CODEX_PACKAGE = "@openai/codex"
OMX_BIN = "omx"
# The installed package's path segment, which SOME argv TOKEN of every OMX-managed hook command carries
# (OMX registers `"<node>" "<prefix>/node_modules/oh-my-codex/dist/scripts/codex-native-hook.js"`, so it
# is the script token, not argv[0]). A token, not a substring of the whole command: a stale fleet entry
# whose trailing comment merely mentions the package must not pass as OMX's.
OMX_HOOK_MARKER = "/node_modules/oh-my-codex/"
# The most of AGENTS.md `needs_converge` reads: the marker sits at its top, and a runner-controlled home
# must never hand the daemon an unbounded read.
_AGENTS_MD_READ_CAP = 64 * 1024
OMX_CONFIG_FILE = ".omx-config.json"
OMX_AGENTS_FILE = "AGENTS.md"
OMX_AGENTS_MARKER = "<!-- omx:generated:agents-md -->"

# Only `ask-user-question` fires: a headless runner stuck on human input is the one event worth paging
# for; everything else is noise. The five events OMX emits (no `agent-call`, unlike OMC).
NOTIFICATION_EVENT_POLICY: dict = {
    "session-start": {"enabled": False},
    "session-stop": {"enabled": False},
    "session-end": {"enabled": False},
    "session-idle": {"enabled": False},
    "ask-user-question": {"enabled": True},
}

# Mirrors the two Claude MCP plugins (`plugin_repair.MCP_PLUGINS`): context7@claude-plugins-official is an
# http server with an optional CONTEXT7_API_KEY bearer; playwright is `npx @playwright/mcp@latest`.
COMPANION_MCP_SERVERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("context7", ("--url", "https://mcp.context7.com/mcp", "--bearer-token-env-var", "CONTEXT7_API_KEY")),
    ("playwright", ("--", "npx", "@playwright/mcp@latest")),
)

_NPM_TIMEOUT_S = 600.0   # two cold pinned installs (pack + `npm ci`); it never waits on the prefix lock
_SETUP_TIMEOUT_S = 120.0  # measured ~1s; generous for a first run
_MCP_TIMEOUT_S = 30.0     # a local config.toml write

_ROOT_REFUSAL = "refusing a root npm install into the runner home"


def _tail(text: object, n: int = 400) -> str:
    return str(text or "")[-n:].strip()


def _env_for(config_dir: str) -> dict:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(config_dir)
    return env


def _call(run, argv: list[str], *, env: dict | None = None, timeout: float | None = None,
          cwd: str | None = None) -> dict:
    """`run` per the module contract, with a raising runner downgraded to exit_code -1."""
    try:
        res = run(argv, env=env, timeout=timeout, cwd=cwd)
    except Exception as exc:
        return {"exit_code": -1, "stdout": "", "stderr": str(exc)}
    if not isinstance(res, dict):
        return {"exit_code": -1, "stdout": "", "stderr": "runner returned no result"}
    return res


# ── packages (once per run, the caller's) ─────────────────────────────────────────────────────────

def refresh_argv(prefix: str) -> list[str]:
    """Raises `runner_cli_pins.PinsError` when either package has no readable pin — never `@latest`."""
    for package in (OMX_PACKAGE, CODEX_PACKAGE):
        runner_cli_pins.spec(package)
    return [sys.executable, "-I", runner_cli_pins.__file__, "install", "--prefix", str(prefix), "--no-wait",
            "--", OMX_PACKAGE, CODEX_PACKAGE]


def refresh_packages(run, *, prefix: str, euid: int | None = None) -> dict:
    """Install omx + codex at their pins under `prefix` (`refresh_argv`; a package already at its pin is
    left alone, and the installer holds the prefix lock). It never queues: a held lock means another
    installer (postinst's arms install the same pins) is at work, so the refresh is skipped rather than
    stall the account converge behind it. Refuses as root (see the module doc).
    `{"applied", "skipped", "error"}`; never raises."""
    if euid is None:
        euid = os.geteuid()
    if euid == 0:
        return {"applied": False, "skipped": _ROOT_REFUSAL, "error": ""}
    try:
        argv = refresh_argv(prefix)
    except runner_cli_pins.PinsError as exc:
        return {"applied": False, "skipped": "", "error": str(exc)}
    res = _call(run, argv, timeout=_NPM_TIMEOUT_S)
    if res.get("exit_code") == runner_cli_pins.EXIT_BUSY:
        return {"applied": False, "skipped": "another installer holds the prefix", "error": ""}
    if res.get("exit_code") != 0:
        return {"applied": False, "skipped": "",
                "error": f"runner_cli_pins install exited {res.get('exit_code')}: {_tail(res.get('stderr'))}"}
    return {"applied": True, "skipped": "", "error": ""}


# ── the per-account layer ─────────────────────────────────────────────────────────────────────────

def setup_argv() -> list[str]:
    return [OMX_BIN, "setup", "--scope", "user"]


def setup_account(config_dir: str, run) -> dict:
    """`omx setup --scope user` into `config_dir` (via CODEX_HOME). `{"applied", "error"}`; never raises.

    Run FROM `config_dir`: even at user scope `omx setup` creates `./.omx/` (setup-scope, HUD config,
    logs) in its working directory, and every caller's own — the packaged tree the sweep runs from, `/`
    under a unit — is unwritable to the account uid, so it died EACCES before writing anything."""
    res = _call(run, setup_argv(), env=_env_for(config_dir), timeout=_SETUP_TIMEOUT_S, cwd=config_dir)
    if res.get("exit_code") != 0:
        return {"applied": False,
                "error": f"omx setup exited {res.get('exit_code')}: {_tail(res.get('stderr'))}"}
    return {"applied": True, "error": ""}


def notification_policy(config_dir: str) -> dict:
    return write_notification_policy(Path(config_dir).expanduser() / OMX_CONFIG_FILE, NOTIFICATION_EVENT_POLICY)


def companion_mcp_argvs(binary: str = "codex") -> list[list[str]]:
    return [[binary, "mcp", "add", name, *args] for name, args in COMPANION_MCP_SERVERS]


def register_companion_mcp(config_dir: str, run, *, binary: str = "codex") -> dict:
    """`codex mcp add` each companion server into `config_dir`'s config.toml.
    `{"registered": [names], "failed": {name: stderr tail}}`; never raises."""
    registered: list[str] = []
    failed: dict[str, str] = {}
    env = _env_for(config_dir)
    for argv in companion_mcp_argvs(binary):
        name = argv[3]
        res = _call(run, argv, env=env, timeout=_MCP_TIMEOUT_S)
        if res.get("exit_code") == 0:
            registered.append(name)
        else:
            failed[name] = _tail(res.get("stderr")) or f"exit {res.get('exit_code')}"
    return {"registered": registered, "failed": failed}


# ── hooks.json (pure; the adapter owns the file) ──────────────────────────────────────────────────

def _entries(value: object) -> list:
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _omx_managed(entry: dict, marker: str) -> bool:
    """OMX owns `entry` when some hook's `command`, split as the shell would (quotes honoured, a `#`
    comment dropped), has a TOKEN carrying `marker`. An unsplittable command (an unbalanced quote) is
    nobody's, so it is dropped like a stale one."""
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        try:
            tokens = shlex.split(str(hook.get("command", "")), comments=True)
        except ValueError:
            continue
        if any(marker in token for token in tokens):
            return True
    return False


def merge_hooks(existing, fleet_hooks: dict, *, marker: str = OMX_HOOK_MARKER) -> dict:
    """PURE. `existing` is the parsed old hooks.json (any JSON value; None/garbage is empty); `fleet_hooks` is
    `{"hooks": {event: [entries]}}` as `agents/codex.py` renders.

    Per event: the fleet's entries FIRST, then the existing entries some token of whose `hooks[*].command`
    carries `marker` (OMX-managed, preserved verbatim; `_omx_managed`). Existing entries without it are the fleet's own stale ones and are
    dropped. Events only `existing` has keep their marked entries (OMX registers PreCompact/PostCompact the
    fleet never does). Key order: fleet events in fleet order, then the rest in existing order; an event
    left with no entries is omitted."""
    fleet = fleet_hooks.get("hooks") if isinstance(fleet_hooks, dict) else None
    fleet = fleet if isinstance(fleet, dict) else {}
    old = existing.get("hooks") if isinstance(existing, dict) else None
    old = old if isinstance(old, dict) else {}
    merged: dict = {}
    for event in [*fleet, *(e for e in old if e not in fleet)]:
        entries = _entries(fleet.get(event)) + [e for e in _entries(old.get(event)) if _omx_managed(e, marker)]
        if entries:
            merged[event] = entries
    return {"hooks": merged}


# ── the scheduling predicate + the converge ───────────────────────────────────────────────────────

def _read_regular_head(path: Path, cap: int = _AGENTS_MD_READ_CAP) -> str | None:
    """The first `cap` bytes of a REGULAR file at `path`, or None when it is absent, a symlink, a FIFO or
    any other non-regular file, or unreadable (`file_io.open_regular`); truncating, not bounded, because a
    marker past the cap is still this file's."""
    fd = file_io.open_regular(str(path))
    if fd is None:
        return None
    try:
        return os.read(fd, cap).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _has_regular_toml(agents_dir: Path) -> bool:
    """A regular `*.toml` directly under `agents_dir`; a symlinked one does not count."""
    try:
        with os.scandir(agents_dir) as entries:
            return any(e.name.endswith(".toml") and e.is_file(follow_symlinks=False) for e in entries)
    except OSError:
        return False


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def needs_converge(config_dir: str) -> str:
    """WHY `config_dir` lacks the OMX layer, or "" when it has it. Read-only, no fork; the FIRST absent marker.

    Absent-only by design: a differing value is the deploy's business, like `provision_accounts.needs_converge`
    (chasing values would re-converge forever). Never reads `hooks.json` (0600, unreadable to the daemon).
    Every probe is no-follow and bounded (`_read_regular_head`, `_has_regular_toml`, `_is_regular`): the
    daemon asks this of a runner-controlled home, so a planted symlink or FIFO reads as ABSENT."""
    if not isinstance(config_dir, str) or not config_dir:
        return ""
    cfg = Path(config_dir).expanduser()
    agents_md = _read_regular_head(cfg / OMX_AGENTS_FILE)
    if agents_md is None:
        return f"{OMX_AGENTS_FILE} is missing"
    if OMX_AGENTS_MARKER not in agents_md:
        return f"{OMX_AGENTS_FILE} lacks the OMX marker"
    if not _has_regular_toml(cfg / "agents"):
        return "agents/ has no *.toml"
    if not _is_regular(cfg / OMX_CONFIG_FILE):
        return f"{OMX_CONFIG_FILE} is missing"
    return ""


def converge_account(config_dir: str, run, *, offline: bool, binary: str = "codex") -> dict:
    """setup → notification policy → companion MCP. `{"setup", "notify", "mcp"}`; never raises.

    `offline` is accepted for symmetry with `plugin_repair`'s converge and changes nothing today: every
    step is a local write (`omx setup` copies from the installed package, `codex mcp add` edits
    config.toml); the only network step, the npm refresh, is the caller's and already gated there."""
    del offline
    setup = setup_account(config_dir, run)
    notify = notification_policy(config_dir)
    mcp = register_companion_mcp(config_dir, run, binary=binary)
    return {"setup": setup, "notify": notify, "mcp": mcp}
