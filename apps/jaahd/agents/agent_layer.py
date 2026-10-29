"""agent_layer.py — THE PROVIDER AGENT LAYER: what a non-Claude agent CLI needs done to one account's
config dir after its credential lands, so its runner can reach the control plane and read the skills.

The provider counterpart of `accounts.py::_finalize_account_add`: two steps — native `mcp add` of the
control-plane server, and the shared skills root. `finalize_provider_account` is the single home.
`place_subagents` is the per-dispatch third (each adapter's `prepare`): the translated subagent roster — and, on a
community-edition node alone, the Claude adapter's copy of the bundle's subagents.

NOT otherwise a Claude path (no `claude` verb: it reads `CLAUDE_CONFIG_DIR`, not `CODEX_HOME`).
NEVER writes a `.claude-plugin/marketplace.json` into a provider home (`jaah_mcp_server_path`).
HUD and notification setup are Claude-only: skipped, not faked. Stdlib + siblings only.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import edition
import plugin_repair  # the install prefix (`MARKETPLACES`, `rehomed`) ONLY — never as a marketplace source
import runner_env  # python_cmd(): the node's own interpreter, never a PATH lookup
import session_user

from . import staged_write
from .base import hub_root

# Outcomes only — never an env overlay, a value from one, or a key list (`run_provider_cli`).
log = logging.getLogger("jaahd.agent_layer")

# A residue, not a budget: finalize runs after `accounts._LOGIN_POLL_TIMEOUT` (120s) inside
# `commands.EXECUTE_TIMEOUT_SECONDS` (170s; CP `COMMAND_TIMEOUT` 180s), leaving 50s. Overrunning shows
# the operator a timeout for an add that then completes invisibly. 30s allows the spool round trip.
PROVIDER_CLI_TIMEOUT_SECONDS = 30.0

# The skills step's share of the same residue (its delegated mkdir otherwise defaults to 30s: 120+30+30
# = 180s overruns). The sum is asserted by `test_the_finalize_sequence_fits_its_budget`.
SKILLS_DIR_TIMEOUT_SECONDS = 15.0

# The name the control-plane MCP server is registered under in EVERY provider's config.
#
# NO UNDERSCORE: a CLI that spells a tool `mcp_<server>_<tool>` cannot tell where an underscored server name
# ends. Every `agent_providers` `mcp_tool_prefix` value derives from this name — rename together.
JAAH_MCP_SERVER_NAME = "control"


def jaah_mcp_server_path() -> str:
    """Absolute path to the control-plane MCP server this node ships.

    Derived from `plugin_repair.MARKETPLACES["jaah"]`, the install prefix's single home, re-rooted at the bundle
    where it is not at the package path (`plugin_repair.rehomed`: Windows, macOS, a community-edition node).

    Absolute, not `${CLAUDE_PLUGIN_ROOT}`: Codex resolves that only for hooks, and it auto-imports any
    `.claude-plugin/marketplace.json` then fails the MCP handshake on it (openai/codex #19372).
    """
    return str(Path(plugin_repair.rehomed(plugin_repair.MARKETPLACES["jaah"])) / "jaah" / "server.py")


# The plugin's own manifest, shipped beside server.py: the one home of how the server is started.
_JAAH_MCP_MANIFEST = Path(__file__).resolve().parent.parent / "mcp" / "jaah" / ".mcp.json"


def jaah_mcp_command() -> tuple[str, ...]:
    """The stdio command that STARTS the control-plane MCP server: the plugin manifest's own, `${CLAUDE_PLUGIN_ROOT}`
    resolved to the installed plugin dir — `python3 <absolute trampoline.py>`, which execs the node's interpreter on
    server.py (`mcp/jaah/trampoline.py`). Exactly the manifest's: Claude Code drops the plugin's server only when an
    entry runs the very same command, else both load and their tool names collide."""
    entry = json.loads(_JAAH_MCP_MANIFEST.read_text(encoding="utf-8"))["mcpServers"][JAAH_MCP_SERVER_NAME]
    root = str(Path(jaah_mcp_server_path()).parent)
    return (entry["command"], *(a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in entry["args"]))


def mcp_add_argv(binary: str, *, scope_args: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The argv that registers the control-plane server with `binary`'s native MCP registry.

    `<binary> mcp add [scope_args] control python3 <trampoline.py>` (`jaah_mcp_command`); a tuple, no shell.

    `scope_args` cannot be inferred: a CLI whose `mcp add` defaults to a project scope writes under `$PWD`,
    the overlay does not reach it, and it exits 0 — leaving no server registered. A parameter the adapter
    supplies, not a `binary ==` branch; empty is right for Codex (no scope option).

    Placed right after `add` by convention only (yargs accepts it anywhere).
    """
    return (binary, "mcp", "add", *scope_args, JAAH_MCP_SERVER_NAME, *jaah_mcp_command())


def run_provider_cli(argv, *, env: dict | None = None, cwd: str = "", input: str | None = None) -> dict:  # noqa: A002
    """Run ONE provider CLI verb AS THE UID THAT OWNS THE ACCOUNT.

    The provider twin of `accounts.py::_claude_cli`: whatever the verb rewrites is owned by whoever ran it,
    so on a uid-split node it is delegated via `session_user.run_as_session_user`; in-process only on a
    one-uid host. A delegation that could not run is a failed verb (`exit_code` -1), never an in-process retry.

    Returns `session_user.capped_result`'s `{"exit_code", "stdout", "stderr"}` on both paths. The
    environment differs: the delegated leg sees only `{overlay, HOME, PATH}` (and never `NODE_TOKEN`); a
    vendor needing a variable names it in its overlay.

    `cwd` is where the verb runs (empty for account-scoped verbs); it exists so callers never restate the
    refusal. ENV IS NEVER LOGGED — the overlay is secret-bearing (`agents/login.py::login_env`). `input` is the
    verb's stdin (a secret a CLI reads there, never on argv); never logged either.
    """
    argv = list(argv)
    overlay = dict(env or {})
    # `can_run_as_session_user()` is False for a one-uid host (run here) AND a split host whose seam is
    # down (refuse — running as `jaahd` would take ownership of the account's config).
    if session_user.switch_needed() and not session_user.can_run_as_session_user():
        return {"exit_code": -1, "stdout": "",
                "stderr": f"refusing to run {argv[0]!r} as this uid: the session-user seam "
                          f"(jaah-runner@<node name>.service and {session_user.exec_spool()}) is down, and running "
                          f"it here would take ownership of this account's config dir. Repair the seam "
                          f"and re-run the account's agent-layer check."}
    if not session_user.can_run_as_session_user():
        try:
            proc = subprocess.run(
                argv, env={**os.environ, **overlay}, cwd=cwd or None, input=input,
                capture_output=True, text=True, timeout=PROVIDER_CLI_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — FileNotFoundError (no binary), TimeoutExpired, OSError
            return {"exit_code": -1, "stdout": "",
                    "stderr": f"could not run {argv[0]!r} ({type(exc).__name__}: {exc})"}
    else:
        try:
            proc = session_user.run_as_session_user(
                argv, env=overlay, cwd=cwd or None, timeout=PROVIDER_CLI_TIMEOUT_SECONDS, input=input)
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch unavailable), TimeoutExpired, OSError
            return session_user.delegation_failure(argv, exc, would_own="this account's config dir")
    return session_user.capped_result(proc)


# `merge_json_settings`' body, run as the account's uid. argv: <agents dir> <path> <patch JSON>, the agents
# dir a `sys.path` entry for `staged_write`. Deep-merges the patch into the file (created when absent, 0600
# from `mkstemp`) and replaces it atomically (`staged_write.replace_file`); refuses, never clobbers, a file
# that is not a JSON object — it is the CLI's general settings file.
_MERGE_JSON_SCRIPT = """
import json, os, sys
agents_dir, path, patch = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.append(agents_dir)   # last, and run `-I`: it shadows nothing on the path
import staged_write
try:
    with open(path) as handle:
        current = json.load(handle)
except FileNotFoundError:
    current = {}
if not isinstance(current, dict):
    sys.exit(path + " is not a JSON object; refusing to rewrite it")
def merge(into, update):
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            merge(into[key], value)
        else:
            into[key] = value
merge(current, patch)
os.makedirs(os.path.dirname(path), exist_ok=True)
staged_write.replace_file(path, json.dumps(current, indent=2))
"""


def merge_json_settings(*, path: str, patch: dict) -> dict:
    """Deep-merge `patch` into the JSON settings file at `path`, as the account's uid (`run_provider_cli`),
    keeping every key already there. `patch` rides argv, so it must hold public literals only — never a
    secret. Non-raising; returns `{"merged": bool, "error": str}`."""
    result = run_provider_cli([runner_env.python_cmd(), "-I", "-c", _MERGE_JSON_SCRIPT,
                               str(Path(__file__).resolve().parent), path, json.dumps(patch)])
    merged = int(result.get("exit_code", -1)) == 0
    return {"merged": merged, "error": "" if merged else str(result.get("stderr") or "").strip()}


def select_api_key_auth(*, config_dir: str, settings_rel_path: str, patch: dict, picker: str,
                        log: Callable[[str], None]) -> None:
    """Merge a vendor's API-key auth selection `patch` into `<config_dir>/<settings_rel_path>` — the one
    `api_key_prepare` body for a vendor whose API-key mode is a settings switch (the adapter's own docstring keeps
    the vendor measurements behind the key). No `config_dir`, no write: the file would otherwise land relative
    to the daemon's cwd. A failed merge is logged through `log` (never raised): the runner will open on the
    vendor's `picker` and never take a turn."""
    if not config_dir:
        return None
    result = merge_json_settings(path=os.path.join(config_dir, settings_rel_path), patch=patch)
    if not result["merged"]:
        log(f"could not select API-key auth in {config_dir} ({result['error']}); this runner will open on "
            f"the {picker} and never take a turn")
    return None


def register_control_mcp(*, binary: str, config_dir: str, env: dict | None = None,
                         scope_args: tuple[str, ...] = ()) -> dict:
    """Register the control-plane MCP server with `binary`'s NATIVE registry for this account (A9).

    Idempotent (`mcp add` replaces a same-name entry), so a repair is re-running `finalize`. Non-raising: a
    failure leaves a usable credential with a degraded agent layer, which an exception would strand.

    The config home is chosen by `env` plus `scope_args` (see `mcp_add_argv`), not `config_dir`; that is
    the account's identity, kept for diagnosis.
    """
    command = mcp_add_argv(binary, scope_args=scope_args)
    result = run_provider_cli(command, env=env)
    code = int(result.get("exit_code", -1))
    return {"registered": code == 0, "command": command,
            "exit_code": code, "stderr": str(result.get("stderr") or "")}


def place_shared_skills(*, config_dir: str) -> dict:
    """Expose the shared cross-vendor skills root inside this account's relocated config home (A10).

    A relocated config home makes a CLI's `homedir()/.agents` skills lookup empty (measured on a
    relocated `GEMINI_CLI_HOME`: no skills discovered); `SKILL.md` is cross-vendor, so one link.

    `ln -sfn` semantics: a bare `os.symlink` would EEXIST on re-run, so `staged_write.replace_symlink` (atomic,
    never resolves through the old link). A real directory at the target is left
    alone and reported. Non-raising. Returns `{placed, path, target, error}`.
    """
    # The SESSION USER's home (the daemon's is /var/lib/jaahd; same dir on a one-uid host). Both reported
    # names are bound before the `try` so the handler cannot raise `UnboundLocalError` when `expanduser`
    # or `Path.home()` raises (`test_the_skills_placement_reports_an_unresolvable_home_rather_than_raising`).
    link = Path(config_dir) / ".agents"
    target = ""
    try:
        link = Path(config_dir).expanduser() / ".agents"
        # Inside the guard: `Path.home()` raises `RuntimeError` with no resolvable home.
        home = session_user.session_home() or str(Path.home())
        shared_root = Path(home) / ".agents"
        target = str(shared_root)
        # Delegated, never an in-process mkdir: it would follow a symlink the session uid planted, and
        # would create the shared skills root owned by `jaahd`. Refuses when the seam is down.
        err = session_user.ensure_dir_as_session_user(str(shared_root / "skills"),
                                                     timeout=SKILLS_DIR_TIMEOUT_SECONDS)
        if err:
            return {"placed": False, "path": str(link), "target": target, "error": err}

        # `is_symlink` before `exists`: `exists()` follows the link and would refuse the idempotent case.
        if not link.is_symlink() and link.exists():
            return {"placed": False, "path": str(link), "target": target,
                    "error": "a real file or directory already exists at this path; left untouched"}
        # Atomic: never a moment with no `.agents`, never a nested `.agents/.agents` in the shared root.
        staged_write.replace_symlink(target, str(link))
    except Exception as exc:  # noqa: BLE001 — OSError family: missing config dir, EACCES, EXDEV
        return {"placed": False, "path": str(link), "target": target,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"placed": True, "path": str(link), "target": target, "error": ""}


# `subagents.py`, run by path (never imported here: it runs at the account's uid) — and its Claude dialect's name.
_SUBAGENTS_SCRIPT = str(Path(__file__).resolve().parent / "subagents.py")
CLAUDE_DIALECT = "claude"


def place_subagents(*, dialect: str, dest: str) -> dict:
    """Translate the node's subagent roster into DEST, in DIALECT (`agents/subagents.py`).

    Sources: the newest OMC plugin installed for ANY Claude account on this node (`agent_config_dirs`),
    otherwise the node's global OMX package, plus the hub's shipped definitions
    (`portable_standard.AGENTS`). Run as the account's own uid (the
    CLI reads DEST; a `jaahd`-owned file there could never be rewritten). Non-raising.
    Returns `{placed, verdict, error}`; a node with no OMC install still places the hub's own.

    DIALECT `claude` is the community edition's: its node is its own harness, so the bundle's `doctrine/agents/*.md`
    (`node/stage_bundle.py`) are copied, under their bare names, into DEST. A cloud node places nothing for Claude
    (`placed` False, no error): its runners get the hub's agents from the fleet harness."""
    if dialect == CLAUDE_DIALECT:
        if not edition.IS_CE:
            return {"placed": False, "verdict": "", "error": ""}
        bundle = Path(hub_root()) / "doctrine" / "agents"
        files = sorted(bundle.glob("*.md"))
        if not files:
            # A converge from nothing would prune every name placed before: keep them, and say so.
            log.warning("provider agent layer: %s holds no subagent, so %s is left as it is", bundle, dest)
            return {"placed": False, "verdict": "", "error": f"{bundle} holds no subagent"}
        return _run_catalogue([runner_env.python_cmd(), _SUBAGENTS_SCRIPT, dialect, dest,
                               *(arg for path in files for arg in ("--agent-file", str(path)))],
                              dest, roster_expected=False)
    try:
        from agent_config_dirs import agent_config_dirs

        roots = list(dict.fromkeys(agent_config_dirs()))
    except Exception as exc:  # noqa: BLE001 — a roster read that failed costs the OMC half only
        log.warning("provider agent layer: could not list this node's Claude account dirs (%s: %s) — "
                    "placing the hub's own subagents only", type(exc).__name__, exc)
        roots = []
    hub = Path(hub_root())
    try:
        # READ, never executed: `tools/` is not on the daemon's path, and exec'ing a file from the hub
        # tree as `jaahd` on every dispatch would be a needless code-execution path in a dev tree.
        import ast

        tree = ast.parse((hub / "tools" / "portable_standard.py").read_text(encoding="utf-8"))
        shipped = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, (ast.Assign, ast.AnnAssign))
                       and any(getattr(t, "id", "") == "AGENTS"
                               for t in (node.targets if isinstance(node, ast.Assign) else [node.target])))
        agent_files = [str(hub / rel) for rel in shipped]
    except Exception as exc:  # noqa: BLE001 — the OMC half still lands without it
        log.warning("provider agent layer: could not read the hub's shipped agent list (%s: %s)",
                    type(exc).__name__, exc)
        agent_files = []
    argv = [runner_env.python_cmd(), _SUBAGENTS_SCRIPT, dialect, dest]
    for root in roots:
        argv += ["--omc-root", root]
    # The npm companion is installed on every node independently of Claude accounts; Codex's own OMX setup
    # may already own overlapping native TOMLs.
    try:
        home = session_user.session_home() or str(Path.home())
        argv += ["--omx-prompts-root", str(Path(home) / ".local/lib/node_modules/oh-my-codex/prompts")]
    except Exception as exc:  # noqa: BLE001 — unavailable home costs only the OMX fallback
        log.warning("provider agent layer: could not locate the node's OMX prompts (%s: %s)",
                    type(exc).__name__, exc)
    for path in agent_files:
        argv += ["--agent-file", path]
    return _run_catalogue(argv, dest)


def _run_catalogue(argv: list[str], dest: str, *, roster_expected: bool = True) -> dict:
    """Run `subagents.py` (ARGV) as the account's uid; its outcome, logged when it failed or found no OMC/OMX
    roster where one is expected."""
    result = run_provider_cli(argv)
    verdict = (result.get("stdout") or "").strip().splitlines()[-1:] or [""]
    placed = int(result.get("exit_code", -1)) == 0
    outcome = {"placed": placed, "verdict": verdict[0],
               "error": "" if placed else str(result.get("stderr") or "").strip()[-500:]}
    if not placed:
        log.warning("provider agent layer: subagent catalogue FAILED for %s (%s) — this runner cannot "
                    "delegate to the review/verify/recon lanes", dest, outcome["error"])
    elif roster_expected and "source=none" in outcome["verdict"]:
        log.warning("provider agent layer: neither OMC nor OMX subagent prompts are installed on this "
                    "node, so %s carries the hub's own subagents only (%s)", dest, outcome["verdict"])
    return outcome


def finalize_provider_account(*, binary: str, config_dir: str, env: dict | None = None,
                              scope_args: tuple[str, ...] = ()) -> dict:
    """THE PROVIDER SEQUENCE: native MCP registration (A9), then skills placement (A10), and NOTHING ELSE.

    Each adapter's `LoginAdapter.finalize` calls this; no adapter adds a step of its own. HUD and
    notification setup are skipped, not faked.

    MCP first (a runner cannot work without it), skills second; neither gates the other.

    Never raises; returns `{"mcp": ..., "skills": ...}` and LOGS any failure itself, since
    `LoginAdapter.finalize` returns None and the caller cannot see the record. The log carries outcome,
    exit code and placement error — never CLI output or env. No provider `account_*_check` verb exists yet.
    """
    # Two statements, not one dict literal, so the order survives someone alphabetising keys.
    mcp = register_control_mcp(binary=binary, config_dir=config_dir, env=env, scope_args=scope_args)
    skills = place_shared_skills(config_dir=config_dir)
    result = {"mcp": mcp, "skills": skills}
    if not result["mcp"]["registered"]:
        log.warning("provider agent layer: MCP registration FAILED for %s (exit %s) — this account is "
                    "registered but its runner cannot reach the control plane",
                    config_dir, result["mcp"]["exit_code"])
    if not result["skills"]["placed"]:
        log.warning("provider agent layer: skills placement FAILED for %s (%s) — this account's runner "
                    "will see an empty skills roster", config_dir, result["skills"]["error"])
    return result
