"""agents/codex.py — the OpenAI Codex CLI as ONE ADAPTER AMONG THE OTHERS.

Everything this daemon knows about the `codex` CLI's grammar lives here; launch sites reach it through
`agents.get_adapter(provider)`. Measured against codex-cli 0.151.0:
  1. Our hooks install ONLY as `$CODEX_HOME/hooks.json` (config.toml tables and `-c` overrides fire nothing).
  2. Hooks run only with `--dangerously-bypass-hook-trust`, and say nothing when they do not.
  3. `$CODEX_HOME/AGENTS.override.md` concatenates ahead of the repo's `AGENTS.md`, not replacing it —
     but it REPLACES `$CODEX_HOME/AGENTS.md` at its own level, which is where oh-my-codex (OMX, the
     Codex twin of OMC) writes its orchestration brain. So the carrier is composed: the charter first
     (it outranks OMX's autonomy directive), then the home's `AGENTS.md` when there is one (`exec_line`).
  4. That flag also runs a worktree's `.codex/hooks.json`, as uid `jaah`, at SessionStart, in no
     transcript — accepted (the runner already executes the repo unsandboxed), logged by `prepare()`.

One account, one hook file and charter: two concurrent runners on one account would overwrite each
other's (bounded today by one runner per account) — and an ACP run cuts that file to OMX's entries
(`_account_hooks_omx_only`), so a legacy and an ACP run sharing an account (only around a runtime switch flip)
can each lose the other's entries. Both files outlive the run; `hooks.json` is inert
outside a fleet dispatch because only `_BASE` carries the trust flag — keep it there. The hook file is
SHARED with OMX (`omx setup` merges its own entries into it), so `write_hooks` merges rather than
rewrites: fleet entries first, then whatever `omx_layer.merge_hooks` recognises as OMX's — as the
account's uid, because the CLI keeps the file 0600 (`_HOOKS_MERGE`). `prepare()` also starts a detached
OFFLINE converge when the OMX layer is absent from the home (`_converge_omx_layer`).
Daemon imports are in-function (`fleet_launch` imports `agents`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from pathlib import Path

import agent_providers  # the provider catalogue: the ONE home of each provider's binary name
import environment  # stdlib-only leaf: the ONE home of the credential socket's variable name
import host_platform  # the OS seam: macOS pins the credential store; Windows hooks: `base.host_hook_commands`
import omx_layer  # the OMX layer's marker, presence check and hook merge (stdlib-only, like the two above)
import provider_health  # the ONE home of this vendor's config-home variable name
import runner_env  # python_cmd(): the node's own interpreter, never a PATH lookup
from acp.events import fold_inclusive_counts

from .base import (
    ACP_HARNESS_EVENTS,
    CLAUDE_ONLY_MATCHERS,
    CONTROL_TOOL_MATCHERS,
    CONTROL_TOOL_NAMES,
    AcpLaunch,
    AcpLaunchContext,
    AdapterDefaults,
    ComposerTui,
    hub_root,
    json_object,
    QuitSignature,
    SessionTreeWalk,
    IncompleteSessionMembers,
    LifecycleChains,
    MAX_TRANSCRIPT_PROBES,
    TASK_TMUX_PASTE,
    TokenCounts,
    TurnError,
    WINDOWS_HOOK_SHELL_POWERSHELL,
    adapter_env,
    chain_hook_groups,
    control_mcp_server,
    count_int,
    credential_file_present,
    hardened_python,
    harness_dispatch,
    host_hook_commands,
    is_folder_slot,
    read_head_lines,
    read_tail_lines,
    tool_path,
    write_rendered_hooks,
)

log = logging.getLogger("jaahd.agents.codex")


# The base invocation every Codex runner execs.
#
# The binary name comes from the catalogue (what `runner_clis.py` attests). The hook-trust flag is
# load-bearing (finding 2); it also admits repo-supplied hooks (finding 4).
_BASE: tuple[str, ...] = (
    agent_providers.CODEX.binary,
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    # No startup update check: an update notice blocks the composer forever. Per-invocation `-c`, so
    # no shared-file write; `--strict-config` confirmed the key exists.
    "-c", "check_for_update_on_startup=false",
    # The subagent tools (`spawn_agent`) the translated catalogue is spawned through (`prepare`). On by
    # default in 0.151.0; pinned per invocation so an account's own config.toml cannot switch it off.
    "-c", "features.multi_agent=true",
    # The structured-question tool (`agent_providers.CODEX.ask_tool`) outside Plan mode: without the
    # feature it answers "unavailable in Default mode" and an autonomous-OFF runner cannot interview. The
    # second key drops the "under-development feature" banner it would otherwise print (codex-cli 0.152.1).
    "--enable", "default_mode_request_user_input",
    "-c", "suppress_unstable_features_warning=true",
    # Tool calls and MCP children under THIS process, never a shared app-server daemon, which would break
    # process-ancestry identity and cross accounts (measured). `features.daemon_auto_start=false` alone still
    # attaches to a running one.
    "--no-daemon",
)


def credential_store_args() -> tuple[str, ...]:
    """`-c cli_auth_credentials_store=file` on macOS, else `()`: the CLI's sign-in pinned to the FILE
    `$CODEX_HOME/auth.json`, never the OS keyring, which a hidden service user has none of and the daemon
    could neither stat (`codex_login.credential_path`) nor link onto a share. `file` is the CLI's default
    (codex-cli 0.163.0 accepts `file|keyring|auto|ephemeral`); the pin keeps an account's config.toml from
    moving it. Rides the login, every launch and console session (`base_argv`), and the API-key login.
    Not the ACP run: `CODEX_CONFIG` reaches `thread/start`, after its app-server has loaded the auth, so
    that run (and `provider_health`'s probe) reads the default store. Bare `file`: a value that is not
    TOML is taken as the string, and the legacy exec line joins argv unquoted."""
    return ("-c", "cli_auth_credentials_store=file") if host_platform.is_macos() else ()


# An API-key launch. The TUI ignores `OPENAI_API_KEY` and `CODEX_API_KEY` alike and opens its sign-in
# screen on a home with no `auth.json` (measured, codex-cli 0.157.1), and `codex login --with-api-key`
# would write the key to disk. A model provider of our own whose `env_key` names the variable needs no
# login at all: measured to skip that screen and send the key as the Bearer on `/v1/responses`. Per
# invocation `-c`, so no shared file is written and a repo's `.codex/config.toml` cannot re-point it. The id
# is ours because a user-defined provider may not reuse a built-in one (`openai`).
_API_KEY_ENV = "OPENAI_API_KEY"
_API_KEY_PROVIDER = "jaah_openai_api_key"
_API_KEY_ARGV: tuple[str, ...] = (
    "-c", f"model_provider={_API_KEY_PROVIDER}",
    "-c", (f'model_providers.{_API_KEY_PROVIDER}={{name="OpenAI",base_url="https://api.openai.com/v1",'
           f'env_key="{_API_KEY_ENV}",wire_api="responses"}}'),
)

# ── The ACP runtime (architecture C11) ──────────────────────────────────────────────────────────────
# Under ACP the engine is `codex-acp` (`@agentclientprotocol/codex-acp`, pinned in
# `packaging/platform-pins.toml`), which drives `codex app-server` over a private stdio child: tool and MCP
# children stay under the caller with no flag (measured). Everything per dispatch rides launch.json's env, so
# no shared file is written:
#   * `CODEX_PATH` — the pinned `codex`, never the adapter's bundled copy (codex-acp 2.2.2 bundles 0.160.1);
#   * `CODEX_CONFIG` — `_BASE`'s `-c`/`--enable` settings as the JSON codex-acp merges into every
#     `thread/start` (plus the dispatch's model);
#   * `INITIAL_AGENT_MODE` — `agent-full-access` (approval `never`, `danger-full-access`): the same posture as
#     `_BASE`'s `--dangerously-bypass-approvals-and-sandbox`.
# The fleet harness runs in the engine's own hook layer (`_acp_harness_hooks`): `CODEX_CONFIG`'s `hooks` (codex-acp
# hands them to `codex app-server -c hooks=`) call the neutral dispatcher on the run's hook file, and its
# `bypass_hook_trust` — `--dangerously-bypass-hook-trust` as the thread config codex-core honours (measured,
# codex-cli 0.163.0; the same key as an app-server `-c` is ignored) — runs them, and the account's `hooks.json`
# (oh-my-codex's entries alone on this runtime, `_ACP_OMX_ONLY`), with no persisted trust. THE FENCE: the bypass
# would also run a repo's `.codex/hooks.json`, so every session root is an UNTRUSTED project in `CODEX_CONFIG`
# (`_untrusted_roots`), which drops the repo's whole `.codex/` layer (its `AGENTS.md` still loads). codex-acp 2.2.2
# marks every session root trusted over that; its fork patch (`packages/acp-forks/codex-acp/`) lets the config win,
# and a node whose installed codex-acp is not that build gets no bypass (`_acp_engine_fences_repo_hooks`): the
# read-only subagent guard alone, TRUSTED BY HASH in the account's config.toml (`_HOOK_TRUST`), as before.
ACP_BINARY = agent_providers.CODEX.acp_binary
#: The npm package `ACP_BINARY` is installed from (`packaging/platform-pins.toml`).
_ACP_PACKAGE = "@agentclientprotocol/codex-acp"
_ACP_PATH_ENV = "CODEX_PATH"
_ACP_CONFIG_ENV = "CODEX_CONFIG"
_ACP_MODE_ENV = "INITIAL_AGENT_MODE"
_ACP_FULL_ACCESS = "agent-full-access"
#: A MicroVM guest's API-key sign-in (`AcpLaunch.authenticate`): codex-acp's `api-key` method, the key in
#: `_meta["api-key"].apiKey` (2.2.2, `authenticateWithApiKey`), which its app-server logs into `$CODEX_HOME`.
ACP_KEY_SIGN_IN = {"method_id": "api-key", "key_meta": "api-key.apiKey"}
_ACP_CONFIG: dict = {
    "check_for_update_on_startup": False,
    "features": {"multi_agent": True, "default_mode_request_user_input": True},
    "suppress_unstable_features_warning": True,
}

# The trust step, run AS THE ACCOUNT'S OWN UID (`_trust_acp_hooks`). argv: <hooks JSON> (`_acp_hooks`). Asks
# the CLI itself — `codex app-server -c hooks=<the same hooks>` — for those hooks' keys and hashes
# (`hooks/list`), then records each session-flag hook whose command is one of ours as trusted
# (`config/value/write`), so the CLI writes its own config.toml. stdout: `trusted=<n>`.
_HOOK_TRUST = r'''
import json, subprocess, sys
hooks = json.loads(sys.argv[1])
ours = {h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]}
def toml(v):
    if isinstance(v, list):
        return "[" + ", ".join(toml(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join("%s = %s" % (json.dumps(k), toml(x)) for k, x in v.items()) + " }"
    return json.dumps(v)
p = subprocess.Popen(["codex", "app-server", "-c", "hooks=" + toml(hooks)], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
def call(i, method, params):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
    p.stdin.flush()
    for line in p.stdout:
        msg = json.loads(line)
        if msg.get("id") == i:
            if "error" in msg:
                raise SystemExit("%s refused: %s" % (method, msg["error"].get("message")))
            return msg["result"]
    raise SystemExit("app-server closed during " + method)
try:
    call(1, "initialize", {"clientInfo": {"name": "jaahd", "version": "0"}})
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
    p.stdin.flush()
    listed = call(2, "hooks/list", {"cwds": ["/"]})
    n = 0
    for entry in listed["data"]:
        for h in entry["hooks"]:
            if h.get("source") == "sessionFlags" and h.get("command") in ours and h.get("trustStatus") != "trusted":
                call(10 + n, "config/value/write", {"keyPath": "hooks.state.%s.trusted_hash" % json.dumps(h["key"]),
                                                    "value": h["currentHash"], "mergeStrategy": "replace"})
                n += 1
    print("trusted=%d" % n)
finally:
    p.kill()
'''

# `LifecycleChains` field -> Codex's own event name, IN THE ORDER THE GENERATED JSON CARRIES THEM
# (`json.dumps` insertion order, which lands in a file on disk).
#
# Each row was observed on a live Codex. `on_error` has no event here
# (no `StopFailure`); `render_hooks` logs it, and the node reports that turn instead (`turn_error`).
_EVENTS: tuple[tuple[str, str], ...] = (
    ("on_session_start", "SessionStart"),
    ("on_turn_end", "Stop"),
    ("on_session_end", "SessionEnd"),
    ("on_prompt_submit", "UserPromptSubmit"),
    ("on_pre_tool", "PreToolUse"),
    ("on_post_tool", "PostToolUse"),
)

# The file Codex loads hooks from, relative to the account's config home. Measured, finding 1.
HOOKS_FILE = "hooks.json"

# ── The fleet harness on the ACP runtime (C6) ───────────────────────────────────────────────────────────
# Codex's hook events ARE Claude's names and its payload is Claude's shape (`tool_name` `Bash` with
# `tool_input.command`, the control server's tools `mcp__control__<tool>`; measured, codex-cli 0.163.0), so the
# per-run hook file (`base.harness_dispatch`) runs through the dispatcher untranslated. Declarations only.
#
# `LifecycleChains` field -> (the hook file's slot, the Claude event the command believes it runs under).
_ACP_CHAIN_EVENTS: tuple[tuple[str, str, str], ...] = tuple((field, event, event) for field, event in _EVENTS)

# Claude Code's harness events -> Codex's: `base.ACP_HARNESS_EVENTS`, but Codex runs `SessionStart` natively (an
# event entry, not the launch's `session_start` slot). No `PostToolUseFailure`: a failed command is a `PostToolUse`,
# which the same memory nudge's `Bash` row already sees.
_HARNESS_EVENTS: dict[str, str | None] = {**ACP_HARNESS_EVENTS, "SessionStart": "SessionStart"}

# Claude's tool matchers -> Codex's, as FULL-match regexes over the payload's `tool_name`. None = dropped.
_HARNESS_MATCHERS: dict[str, str | None] = {
    # Codex edits through `apply_patch` (not measured as a hook's tool name, so its Claude aliases match too).
    "Edit|Write|MultiEdit": "apply_patch|Edit|Write",
    "Bash": "Bash",
    # No Read tool: Codex reads through its shell, which the `Bash` half already guards.
    "Read|Bash": "Bash",
    # The project-memory read path: no Read, Grep or Glob tool to fire on.
    "Read|Grep|Glob": None,
    # The ACP host files the engine's own question (`request_user_input`, an elicitation) as the operator round.
    "AskUserQuestion": None,
    **CLAUDE_ONLY_MATCHERS,
    **CONTROL_TOOL_MATCHERS,
}

# Codex's control-server tool names -> Claude's, so the settle guard reads the name it compares (`dispatch`).
_TOOL_NAMES: dict[str, str] = CONTROL_TOOL_NAMES

# The per-run hook file, `<config home>/<stem>-<session id>.json`; files older than the TTL are pruned when a
# run writes its own (`_RUN_HOOKS_WRITE`).
_RUN_HOOKS_STEM = "jaah-acp-hooks"
_RUN_HOOKS_TTL_DAYS = 30
# A dispatch entry's wall clock when it declares none: `hook_bridge_core.py::DEFAULT_TIMEOUT_SECONDS` (a
# test pins them equal). The engine's budget for one event's dispatch is the sum over its entries, which run in turn.
_DISPATCH_ENTRY_SECONDS = 30

# The charter carrier (finding 3). The override name is read INSTEAD of `AGENTS.md` at its level, so the
# home's own `AGENTS.md` (OMX's) is folded in behind the charter rather than shadowed by it.
CHARTER_FILE = "AGENTS.override.md"

# Where `_HOOKS_MERGE` finds `omx_layer` when it runs at the account's uid: this deployable's `jaahd`
# root (`/usr/lib/jaahd` under the package, the checkout in a dev tree) — the interpreter over there
# has no `agents` package on its path.
_OMX_LAYER_DIR = str(Path(__file__).resolve().parent.parent)
# Where `_MCP_ENV_PASSTHROUGH` finds `staged_write` at the account's uid (the same reason).
_AGENTS_DIR = str(Path(__file__).resolve().parent)

# When a detached OMX converge was last started per config home (`_converge_omx_layer`), so repeat
# dispatches within `provision_accounts.CONVERGE_COOLDOWN_SECONDS` do not start another. Absence is
# not a state: `.get` then `is None`, never a default epoch.
_omx_converge_started_at: dict[str, float] = {}
# Why a Windows node never lays the OMX layer: oh-my-codex is an npm package, and no Node.js runtime ships there.
_OMX_WINDOWS_SKIP = "oh-my-codex is not installed on Windows nodes (no Node.js runtime there)"

# An operator quit, as this CLI's `SessionEnd` shows it (codex-cli 0.157.1). `other` is EVERY orderly ending — `/exit`, `/quit`, Ctrl-C twice, Ctrl-D, and also
# `/new`, which keeps the process and starts another conversation — and a signal fires no `SessionEnd`
# at all. What separates a quit is the composer, which reads `› Shutting down...` while the hook runs
# (measured on `/exit`, Ctrl-C twice and Ctrl-D) and is the ordinary empty composer after `/new`. The hook
# matches it only at a line start near the bottom of the pane, and the model's own lines are prefixed `•`.
_QUIT = QuitSignature(reason="other", pane_marker="\u203a Shutting down")

# `sessions/<YYYY>/<MM>/<DD>/rollout-<ISO8601>-<session id>.jsonl` (from hook `transcript_path`). Only
# the leaf is spelled; date directories are walked, never composed.
_SESSIONS_DIR = "sessions"
# The trailing uuid of a rollout stem, anchored on the uuid shape. `\Z`, NOT `$`: `$` matches before a
# trailing newline, and this pattern gates a RUNNER-REPORTED id before it reaches a glob.
_ROLLOUT_ID_RE = re.compile(
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\Z"
)

# Head read for `session_meta` (measured 21 KB, it carries the base instructions); generous per line so
# truncation cannot lose attribution, a few lines in case the CLI writes something ahead of it.
_META_LINE_BYTES = 512 * 1024
_META_HEAD_LINES = 8

# THE PER-CALL BYTE BUDGET: the bounds above are per line/file, and without this one call can read
# 4 MiB x 256 probes (~1 GiB) off hardlinked padded files. Floor: a legitimate full probe is ~5.4 MB, so
# 32 MiB is ~6x headroom for a growing charter. Ceiling: at most 8 padded candidates, well under a second.
# Checked before a candidate, charged after, so one head may overshoot — never truncate a real header.
_META_CALL_BYTES = 32 * 1024 * 1024

# How much of a rollout's END `turn_error` reads. A turn's closing `task_complete` is its last record, so
# only the tail matters; big enough to hold one oversized tool-output line after it.
_TURN_TAIL_BYTES = 256 * 1024

# `sessions/YYYY/MM/DD/rollout-*.jsonl` — the root counts as level 1, so the files are at level 4.
_ROLLOUT_DEPTH = 4

# The TUI's vocabulary, read off a live pane; `answer_delivery` owns the vendor-neutral paste mechanism.
_TUI = ComposerTui(
    # The empty composer's placeholder, not the `›` chevron (which also prefixes menu rows).
    ready_marker="Ask Codex to do anything",
    # `• Working (0s • esc to interrupt)`: the composer stays rendered while busy (`ComposerTui.is_ready`).
    busy_marker="Working (",
    # A collapsed paste; its presence proves the paste landed.
    paste_placeholder=re.compile(r"\[Pasted Content \d+ chars\]"),
    # The `request_user_input` picker's footer (`tab to add notes | enter to submit answer | esc to …`).
    dialog_marker="enter to submit answer",
)


def _fleet_tool_command(script_name: str, log_name: str) -> str:
    """A fleet `tools/` hook, as the same fail-open command Claude's harness row runs
    (the fleet harness's static hook layer), resolved against this install's own root. Its log lands in
    the account home (never the repo tree, whose `.omc/` a repo could point elsewhere), else nowhere — a
    failed redirect would skip the hook."""
    import fleet_dispatch  # noqa: PLC0415 — in-function, as every daemon import here
    from fleet_launch import shq  # noqa: PLC0415

    script = shq(str(fleet_dispatch.HUB_ROOT / "tools" / script_name))
    home = provider_health.CODEX_HOME_ENV
    log_to = f'"$([ -n "${home}" ] && echo "${home}/{log_name}" || echo /dev/null)"'
    return f"{shq(runner_env.python_cmd())} {script} 2>>{log_to} || true"


def _ask_capture_command() -> str:
    """The `PreToolUse` command that captures a `request_user_input` call into `task_questions`."""
    return _fleet_tool_command("capture_askuserquestion.py", "capture-askuserquestion.log")


def _ask_nudge_command() -> str:
    """The `Stop` command that sends a turn ending in a prose ask back once, toward the question tool."""
    return _fleet_tool_command("check_unattended_ask_nudge.py", "unattended-ask-nudge.log")


def _readonly_guard_command() -> str:
    """The CLI hook that refuses writes from read-only native subagents."""
    import fleet_dispatch  # noqa: PLC0415
    from fleet_launch import shq  # noqa: PLC0415

    script = shq(str(fleet_dispatch.HUB_ROOT / "tools" / "codex_subagent_guard.py"))
    return f"{shq(runner_env.python_cmd())} -I {script}"


def _acp_hooks() -> dict:
    """The fleet's hooks on an ACP run with no trust bypass (`_acp_engine_fences_repo_hooks`): the read-only
    subagent guard alone."""
    return {"PreToolUse": [{"hooks": [{"type": "command", "command": _readonly_guard_command()}]}]}


# This adapter's hook declarations, rendered by the neutral `base.harness_dispatch`.
def _dispatch_hooks(chains: LifecycleChains) -> dict:
    """The run's hook file (`base.harness_dispatch`): its chains, then the fleet harness, memory rows included."""
    return harness_dispatch(chains, vendor=agent_providers.CODEX.value, chain_events=_ACP_CHAIN_EVENTS,
                            events=_HARNESS_EVENTS, matchers=_HARNESS_MATCHERS, tool_names=_TOOL_NAMES)


def _run_hooks_path(config_dir: str, session_id: str) -> str:
    """This run's hook file in the account's config home; the session id kept to a filename's safe characters."""
    return str(Path(config_dir) / f"{_RUN_HOOKS_STEM}-{re.sub(r'[^A-Za-z0-9._-]', '_', session_id)}.json")


def _acp_harness_hooks(hooks_file: str, dispatch: dict) -> dict:
    """The engine's own hook layer for one run: per event the run's hook file has entries for, ONE group running
    the neutral dispatcher on Claude's wire (`hook_bridge_core.py dispatch --wire claude`, which Codex reads
    natively), its budget every entry's own; then the read-only subagent guard, as `render_hooks` adds it."""
    hooks: dict = {}
    for event, entries in dispatch["events"].items():
        command = hardened_python(tool_path("hook_bridge_core.py"), "dispatch", "--event", event, "--wire", "claude",
                                  "--hooks-file", hooks_file)
        budget = sum(e.get("timeout") or _DISPATCH_ENTRY_SECONDS for e in entries)
        hooks[event] = [{"hooks": [{"type": "command", "command": command, "timeout": budget}]}]
    hooks.setdefault("PreToolUse", []).append({"hooks": [{"type": "command", "command": _readonly_guard_command()}]})
    return hooks


def _untrusted_roots(ctx: AcpLaunchContext) -> dict:
    """`CODEX_CONFIG`'s `projects`: every session root (the worktree and each additional directory, as given and
    resolved) an untrusted project, so a repository's own `.codex/` layer — its `hooks.json` above all — never loads
    under the trust bypass."""
    roots = (r for d in (ctx.worktree, *ctx.additional_dirs) if d for r in (d, os.path.normpath(d), os.path.realpath(d)))
    return {root: {"trust_level": "untrusted"} for root in dict.fromkeys(roots)}


def _acp_engine_fences_repo_hooks() -> bool:
    """Whether this node's `codex-acp` is the pinned build WITH its fork patches (`runner_cli_pins.stale`, under the
    runner's npm prefix `~/.local`): only then does `_untrusted_roots` reach the engine. False when it cannot tell, and
    when the package has no pin (`stale` skips an unpinned name)."""
    import runner_cli_pins  # noqa: PLC0415 — daemon modules, in-function (see the header)
    import session_user  # noqa: PLC0415

    home = session_user.session_home()
    if not home:
        return False
    try:
        return _ACP_PACKAGE in runner_cli_pins.load_npm() and not runner_cli_pins.stale(Path(home) / ".local",
                                                                                         [_ACP_PACKAGE])
    except Exception as exc:  # noqa: BLE001 — unknowable is not fenced
        log.warning("codex acp: could not tell whether %s is the patched pin (%s: %s)", _ACP_PACKAGE,
                    type(exc).__name__, exc)
        return False


def _mcp_server_table() -> str:
    """The TOML table header `mcp add` writes for the control-plane server, from
    `agent_layer.JAAH_MCP_SERVER_NAME` (never re-spelled)."""
    from . import agent_layer

    return f"mcp_servers.{agent_layer.JAAH_MCP_SERVER_NAME}"


# The variables the control-plane server must INHERIT from the runner's own environment. This CLI starts a
# stdio MCP server with a filtered env (HOME, PATH, USER, … — measured on 0.162.1), so a co-located node's
# `JAAH_CRED_SOCKET` never reached the server, which then dialled the default socket and every control
# call failed. Forwarded BY NAME through the table's `env_vars` key, never as a value: the registration is
# per ACCOUNT and an account is shared across co-located nodes, each with its own socket.
#
# `JAAH_PROJECT_MEMORY` too: the server publishes the `memory_*` tools only when the session's memory is on
# (`mcp/jaah/server.py::_MEMORY_ENV`, `project_memory_bundle.ENV_BUNDLE`). An account converged with the
# earlier one-name list is upgraded in place (`_MCP_ENV_PASSTHROUGH`).
_MCP_ENV_VARS = (environment.CRED_SOCKET_ENV, "JAAH_PROJECT_MEMORY")


# The `lock(file)` both config.toml scripts below take, exclusive and blocking: `flock`, or on Windows (no
# `fcntl`) `msvcrt`'s one-byte lock, which retries for ~10 s and then raises — a converge that loses that
# race fails like any other and is re-done on the next dispatch.
_EXCLUSIVE_LOCK = r'''
try:
    import fcntl

    def lock(fh):
        fcntl.flock(fh, fcntl.LOCK_EX)
except ImportError:                                     # Windows
    import msvcrt

    def lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
'''


# The converge script, run AS THE ACCOUNT'S OWN UID (`_converge_account_config`). argv: <config.toml>
# <abs worktree> <mcp table path>; stdout is a one-line verdict. Append-only under an exclusive lock
# (serialises converges only, not the CLI's own rewrites — a lost append is re-done next dispatch). An
# unparseable file is left alone. Every append is re-parsed and TRUNCATED back on failure: some legal
# spellings (`projects = {...}`, a non-table) make an appended `[projects."…"]` unparseable forever.
_CONFIG_CONVERGE = r'''
import json, os, sys
''' + _EXCLUSIVE_LOCK + r'''
path, worktree, mcp_table = sys.argv[1], sys.argv[2], sys.argv[3]
env_vars = json.loads(sys.argv[4]) if len(sys.argv) > 4 else []
try:
    import tomllib
except ImportError:                                     # pre-3.11 interpreter
    # NOT `trust=unparseable`: nothing was parsed and nothing is wrong with the file. Its own verdict, so
    # the caller can say which of the two happened — a runner that hangs on a trust modal because this
    # node's python is too old is a different diagnosis from one whose config is corrupt.
    print("trust=no-tomllib mcp=unknown")
    print("no tomllib on %s — cannot converge %s" % (sys.version.split()[0], path), file=sys.stderr)
    sys.exit(0)


def table(data, name):
    """`data[name]` when it is a real TOML table, else an empty one. A `key in <str>` on a wrongly-typed
    value is a SUBSTRING test, which would report a registration that is not there."""
    value = data.get(name)
    return value if isinstance(value, dict) else {}


os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
with open(path + ".jaahd.lock", "a") as lockfile:
    lock(lockfile)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        data = {}
    except Exception as exc:
        print("trust=unparseable mcp=unknown")
        print("could not parse %s: %s" % (path, exc), file=sys.stderr)
        sys.exit(0)
    section, _, key = mcp_table.partition(".")
    entry = table(data, section).get(key)
    if not isinstance(entry, dict):
        mcp = "absent"
    else:
        # `stale`: registered, but not forwarding every variable `env_vars` names (a pre-passthrough
        # registration, or an `mcp add` re-run, which rewrites the table without the key).
        forwarded = entry.get("env_vars")
        forwarded = forwarded if isinstance(forwarded, list) else []
        mcp = "present" if all(name in forwarded for name in env_vars) else "stale"
    if worktree in table(data, "projects"):
        print("trust=present mcp=%s" % mcp); sys.exit(0)
    # ONE HANDLE ACROSS THE APPEND AND THE ROLLBACK, and the length taken FROM IT. A `stat()` before the
    # write plus a fresh `open(path, "r+")` after it are two different questions about two possibly
    # different inodes: the header above says in its own words that this lock cannot serialise the CLI's
    # own temp-and-rename, so a rename landing in between would have the rollback truncate the NEW file
    # to the OLD file's length — and `truncate(n)` with n LARGER than the file EXTENDS it with NUL bytes,
    # producing exactly the permanently-unparseable `config.toml` this rollback exists to prevent.
    # `fstat` on the same fd is also what makes the clamp below meaningful.
    with open(path, "a+") as fh:
        fh.seek(0, 2)
        before = fh.tell()
        fh.write("\n[projects.%s]\ntrust_level = \"trusted\"\n" % json.dumps(worktree))
        fh.flush()
        try:
            with open(path, "rb") as check:
                tomllib.load(check)
        except Exception as exc:
            # THE ROLLBACK. The append was legal TOML on its own and illegal after what was already
            # there; keeping it would cost this account its MCP registration and its model provider
            # permanently. CLAMPED to the file's CURRENT length so this can only ever SHRINK: if
            # something replaced the file underneath us, the honest answer is to leave it alone rather
            # than pad it out to a length it never had.
            fh.truncate(min(before, os.fstat(fh.fileno()).st_size))
            print("trust=unappendable mcp=%s" % mcp)
            print("appending a trust record to %s would make it unparseable (%s) — rolled back"
                  % (path, exc), file=sys.stderr)
            sys.exit(0)
    try:
        # Group-READABLE so anything else in the `jaah` group can still read this account's config.
        # Not load bearing — every check above happens at this uid precisely so it need not be — but a
        # 0600 file here is the shape that has silently broken a daemon-side read before. NOT 0o660: this
        # file names the command the account's runners exec for MCP, and a group write on it is a wider
        # grant than the read the problem actually needs.
        os.chmod(path, 0o640)
    except OSError:
        pass
    print("trust=appended mcp=%s" % mcp)
'''

# The env-passthrough script, run AS THE ACCOUNT'S OWN UID right after `codex mcp add` (`register_mcp`),
# which rewrites the table without the key. argv: <agents dir> <config.toml> <mcp table path> <JSON list of
# names> (the agents dir a `sys.path` entry, for `staged_write`);
# stdout is ONE verdict: `env=present|patched|absent|conflict|unpatchable|raced`. Inserts `env_vars = [...]` on
# the line after the table's own header, under the converge's lock, then PROVES it: the result must parse
# and carry exactly those names, or nothing is written. Written by `staged_write.replace_file` at the file's
# own mode and group, so a runner never reads half a file.
_MCP_ENV_PASSTHROUGH = r'''
import json, os, sys
''' + _EXCLUSIVE_LOCK + r'''
agents_dir, path, mcp_table, names = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
sys.path.append(agents_dir)
import staged_write
try:
    import tomllib
except ImportError:
    print("env=unpatchable")
    print("no tomllib on %s — cannot patch %s" % (sys.version.split()[0], path), file=sys.stderr)
    sys.exit(0)

section, _, key = mcp_table.partition(".")


def entry_of(data):
    value = data.get(section)
    value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else None


with open(path + ".jaahd.lock", "a") as lockfile:
    lock(lockfile)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        entry = entry_of(tomllib.loads(raw.decode("utf-8")))
    except FileNotFoundError:
        entry = None
    except Exception as exc:
        print("env=unpatchable")
        print("could not parse %s: %s" % (path, exc), file=sys.stderr)
        sys.exit(0)
    if entry is None:
        print("env=absent"); sys.exit(0)
    have = entry.get("env_vars")
    upgrade = False
    if have is not None:
        if isinstance(have, list) and all(name in have for name in names):
            print("env=present"); sys.exit(0)
        if not (isinstance(have, list) and all(name in names for name in have)):
            # Someone else's `env_vars`: a second key would be a duplicate and break the whole file.
            print("env=conflict")
            print("%s already sets env_vars = %r in [%s]" % (path, have, mcp_table), file=sys.stderr)
            sys.exit(0)
        upgrade = True   # an earlier, shorter list of ours: replaced by the whole one
    lines = raw.decode("utf-8").splitlines(keepends=True)
    header = "[%s]" % mcp_table
    at = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if at is None:
        print("env=unpatchable")
        print("no plain %s header in %s" % (header, path), file=sys.stderr)
        sys.exit(0)
    if upgrade:
        end = next((i for i in range(at + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        old = [i for i in range(at + 1, end) if lines[i].lstrip().startswith("env_vars")]
        if len(old) != 1 or not lines[old[0]].rstrip().endswith("]"):
            print("env=unpatchable")
            print("env_vars in [%s] of %s is not one line — left alone" % (mcp_table, path), file=sys.stderr)
            sys.exit(0)
        del lines[old[0]]
    if not lines[at].endswith("\n"):
        lines[at] += "\n"
    lines.insert(at + 1, "env_vars = %s\n" % json.dumps(names))   # a JSON string list is TOML too
    text = "".join(lines)
    try:
        proven = (entry_of(tomllib.loads(text)) or {}).get("env_vars") == names
    except Exception:
        proven = False
    if not proven:
        print("env=unpatchable")
        print("inserting env_vars into %s would not parse as intended — left alone" % path,
              file=sys.stderr)
        sys.exit(0)
    if os.path.islink(path):
        print("env=unpatchable")
        print("%s is a symlink; replacing it would cut the link — left alone" % path, file=sys.stderr)
        sys.exit(0)
    st = os.stat(path)
    # The lock cannot serialise the CLI's own rewrites: if one landed since the read, write nothing rather
    # than lose it — the next dispatch's converge reports `stale` and this runs again. Kept at the file's
    # own mode and group (a `jaah`-group reader needs it).
    if not staged_write.replace_file(path, text.encode("utf-8"), mode=st.st_mode & 0o777, gid=st.st_gid,
                                     unchanged_from=raw):
        print("env=raced")
        print("%s changed while being patched — left alone" % path, file=sys.stderr)
        sys.exit(0)
    print("env=patched")
'''

# The hook-merge script, run AS THE ACCOUNT'S OWN UID (`write_hooks`): the CLI keeps `hooks.json` 0600,
# so the daemon cannot read what OMX put there. argv: <omx_layer dir> <hooks.json> <fleet hooks JSON>
# <OMX marker>. Nothing in argv is code — the JSON is `json.loads`'d, the paths are paths, the layer
# dir (and its `agents/`, for `staged_write`) a `sys.path` entry. A missing, unopenable (any `OSError`) or unparseable file merges as `None`
# (the fleet's entries alone) rather than traceback into `write_hooks`'s clobbering fallback, and the
# verdict says which it was. Written 0600 by `staged_write.replace_file`, so a runner never reads a
# half-written file. Stdout is a one-line verdict:
# `hooks=merged kept=<n> existing=<read|missing|unreadable>`.
_HOOKS_MERGE = r'''
import json, os, sys

layer_dir, path, fleet_json, marker = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, layer_dir)
sys.path.append(os.path.join(layer_dir, "agents"))
import omx_layer
import staged_write

fleet = json.loads(fleet_json)
try:
    with open(path, "rb") as fh:
        existing = json.load(fh)
    state = "read"
except FileNotFoundError:
    existing, state = None, "missing"
except (OSError, ValueError):
    existing, state = None, "unreadable"
merged = omx_layer.merge_hooks(existing, fleet, marker=marker)
kept = 0
for entries in (merged.get("hooks") or {}).values():
    for entry in entries or ():
        commands = [h.get("command") for h in (entry.get("hooks") or ()) if isinstance(h, dict)]
        if any(isinstance(c, str) and marker in c for c in commands):
            kept += 1
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
staged_write.replace_file(path, json.dumps(merged, indent=2) + "\n", mode=0o600)
print("hooks=merged kept=%d existing=%s" % (kept, state))
'''

# `_HOOKS_MERGE`'s fleet argument on the ACP runtime: no fleet entries, so the account's `hooks.json` keeps
# oh-my-codex's alone. The trust bypass runs that file, and a legacy dispatch's chains left in it would claim,
# heartbeat and settle beside the host (`_acp_prepare`).
_ACP_OMX_ONLY = json.dumps({"hooks": {}})

# The run hook file's write, run AS THE ACCOUNT'S OWN UID (`_acp_prepare`), so the runner's dispatcher can read
# it. argv: <agents dir> <path> <content> <stem> <ttl days>. Prunes the account's run hook files older than the
# TTL, then writes this run's 0600 by `staged_write.replace_file`. stdout: `hooks-file=written`.
_RUN_HOOKS_WRITE = r'''
import glob, os, sys, time

agents_dir, path, content, stem, ttl_days = sys.argv[1:6]
sys.path.insert(0, agents_dir)
import staged_write

cutoff = time.time() - float(ttl_days) * 86400
for old in glob.glob(os.path.join(glob.escape(os.path.dirname(path)), stem + "-*.json")):
    try:
        if os.lstat(old).st_mtime < cutoff:
            os.unlink(old)
    except OSError:
        pass
if not staged_write.replace_file(path, content, mode=0o600):
    raise SystemExit("raced: the run hook file changed under the write")
print("hooks-file=written")
'''


class IncompleteRolloutWalk(Exception):
    """THIS READER COULD NOT COMPLETE ITS LOOK — as distinct from completing it and finding nothing.

    `None` from the readers means "walked the whole tree, no transcript", which `session_sweep` reports as
    the TERMINAL `unavailable`. An incomplete walk (scan cap, probe cap, head-byte budget) is manufacturable
    by a co-tenant for one tick and then reverted, so answering `None` would permanently retire a session
    whose rollout is on disk. Raising lands in `session_sweep`'s `VendorLookupFailed` → retryable
    `STATUS_FAILED`. A deliberate extension of `base.AgentAdapter`'s "or None" contract; both callers guard it.
    """


class CodexAdapter(AdapterDefaults):
    """The `codex` CLI. Stateless — the registry holds one instance."""

    provider = agent_providers.CODEX.value
    # `account_layer()` "omx", not "omc", is a measured REFUSAL: this CLI auto-imports the layer's
    # `.claude-plugin/marketplace.json` and fails the MCP handshake on it (openai/codex #19372).
    # `session_binding` DISCOVER: no `--session-id` flag; the discovered id is for resume/transcripts only.

    # ── Launch ─────────────────────────────────────────────────────────────────────────────────────

    base_command = _BASE

    def base_argv(self, *, model: str) -> tuple[str, ...]:
        """`AdapterDefaults.base_argv` plus `credential_store_args()`."""
        return super().base_argv(model=model) + credential_store_args()

    #: PASTED into the live pane (architecture A6) — see `exec_line` for why it is not on the argv.
    task_delivery_kind = TASK_TMUX_PASTE
    #: This CLI's on-screen vocabulary, for the paste `task_delivery` asks for. See `_TUI`.
    tui_vocabulary = _TUI
    #: `OPENAI_API_KEY`, the variable `_API_KEY_ARGV`'s provider names.
    api_key_variable = _API_KEY_ENV
    #: `other` plus the composer's `› Shutting down` line (`_QUIT`).
    quit_sig = _QUIT
    #: `codex mcp add control <node interpreter> <absolute server.py>`, no scope flag — see `register_mcp`.
    mcp_binary = agent_providers.CODEX.binary

    def exec_line(
        self, *, sys_file: str, task_file: str, settings_flag: str,
        model_args: tuple[str, ...] = (), session_args: tuple[str, ...] = (), rc_flag: str = "",
    ) -> list[str]:
        """Codex's whole exec block for a dispatch's `launch.sh`.

        The charter is INSTALLED, not passed (no `--append-system-prompt`): composed into
        `$CODEX_HOME/AGENTS.override.md` here, at the runner's uid, guarded so an account-less dispatch
        does not write to `/`. Composed, not copied (finding 3): the override REPLACES the home's own
        `AGENTS.md`, which is OMX's, so the installed file is the charter, then — only when
        `$CODEX_HOME/AGENTS.md` is a regular file — a blank line and that file. Charter first so it
        outranks OMX's autonomy directive. Built in a temp next to the destination and `install`ed
        over it, never `cat >`'d onto it (a runner reading mid-write). The task prompt is pasted later
        (`task_delivery`), so `task_file` is unused. `settings_flag` is empty for this vendor. Every
        interpolated element is `shq`'d — `projects.default_model` is operator-editable and reaches an
        executed file."""
        from fleet_launch import shq  # in-function: fleet_launch imports this package (see the header)

        quoted = " ".join(shq(a) for a in (*model_args, *session_args))
        rc = shq(rc_flag) if rc_flag else ""
        exec_argv = " ".join(self.base_argv(model=""))
        # `${CODEX_HOME:-}` survives `set -u`; every path is double-quoted, the source `shq`'d, and every
        # `cat`/`install`/`rm` takes `--` so no path is read as an option. `[ -f ]` follows symlinks on
        # purpose: a symlink here was planted by this same uid (the runner owns the home), so it is not
        # a trust boundary — only a directory, a FIFO or a dangling link is refused, because `cat` on
        # those fails or never returns. The temp carries `$$` so two launches on one home cannot share it,
        # and is born 0600 (`umask 077` in a subshell, so the runner's own umask never widens it before
        # `install -m 600`). A failed `cat` of the charter breaks the `&&` chain before `install`, so a
        # partial override is never installed; the line's exit status is `rm -f`'s (no `set -e` here).
        home = f"${provider_health.CODEX_HOME_ENV}"
        tmp = f'"{home}/{CHARTER_FILE}.tmp$$"'
        charter = (
            f'if [ -n "${{{provider_health.CODEX_HOME_ENV}:-}}" ]; then '
            f"( umask 077 && cat -- {shq(sys_file)} > {tmp} ) && "
            f'{{ if [ -f "{home}/{omx_layer.OMX_AGENTS_FILE}" ]; then '
            f"printf '\\n' >> {tmp} && cat -- \"{home}/{omx_layer.OMX_AGENTS_FILE}\" >> {tmp}; fi; }} && "
            f'install -m 600 -- {tmp} "{home}/{CHARTER_FILE}"; '
            f"rm -f -- {tmp}; fi"
        )
        return [
            charter,
            " ".join(p for p in (f"exec {exec_argv}", rc, quoted, settings_flag) if p),
        ]

    def resume_exec_line(self, *, sys_file: str, settings_flag: str, model_args: tuple[str, ...],
                         native_ref: str) -> list[str]:
        """`exec_line` reopening rollout `native_ref`: the same charter install, then `codex <root flags> resume
        <ref>` — the `resume` subcommand merges the root flags (model, sandbox, approvals) into the resumed session,
        and takes no prompt, so the TUI opens on the conversation as the chat left it."""
        return self.exec_line(sys_file=sys_file, task_file="", settings_flag=settings_flag, model_args=model_args,
                              session_args=("resume", native_ref))

    def inherits_harness_hooks(self) -> bool:
        """False: the fleet's account hook layer is Claude-format, so the gate is composed into the chains
        instead (`fleet_launch.vis_quick_gate_cmd`)."""
        return False

    def api_key_argv(self) -> tuple[str, ...]:
        """`_API_KEY_ARGV`: the key read from the environment, never written to `auth.json`."""
        return _API_KEY_ARGV

    def account_layer_refresh(self, run, *, prefix: str) -> dict:
        """The OMX layer's packages — oh-my-codex and this CLI itself, each at its pin — under `prefix`.
        `omx_layer.refresh_packages` is the one spelling (and the root refusal is its own). Skipped on
        Windows: oh-my-codex is an npm package and a Windows node carries no Node.js runtime."""
        if host_platform.is_windows():
            return {"applied": False, "skipped": _OMX_WINDOWS_SKIP, "error": ""}
        return omx_layer.refresh_packages(run, prefix=prefix)

    def account_layer_converge(self, *, config_dir: str, run, offline: bool) -> dict:
        """The OMX layer into this home: `omx setup`, the notification policy, the companion MCP servers
        (`omx_layer.converge_account`). `present` is `needs_converge` re-asked after the writes, so a
        setup that exited 0 and still left no marker reads absent; `error` is the setup's, if any."""
        result = omx_layer.converge_account(config_dir, run, offline=offline)
        return {"present": omx_layer.needs_converge(config_dir) == "",
                "error": str(result.get("setup", {}).get("error") or ""), **result}

    def account_layer_missing(self, *, config_dir: str) -> str:
        """`omx_layer.needs_converge` — the one home of the presence question (`_converge_omx_layer` asks
        the same one at dispatch)."""
        return omx_layer.needs_converge(config_dir)

    def env(self, *, config_dir: str, worktree: str = "") -> dict[str, str]:
        """`CODEX_HOME` — the one variable that moves this CLI onto an account.

        A whole-home relocation (config, credential, sessions). Spelling from `provider_health.CODEX_HOME_ENV`.
        Omitted, never empty, with no account. `worktree` is unread: isolation is the home alone."""
        return adapter_env(config_dir, {provider_health.CODEX_HOME_ENV: config_dir})

    def windows_cli_dir(self) -> str:
        """`<bundle>\\codex\\bin`, where a Windows node's bundle ships this CLI (the package archive pinned in
        `packaging/platform-pins.toml`, unpacked whole so `codex.exe` keeps its sibling resources). Read by
        `runner_clis.windows_cli_dirs`: launch.sh's PATH and the node's attestation search the same dir."""
        import ntpath  # noqa: PLC0415 — Windows-only, as the paths below

        from host_platform import windows  # noqa: PLC0415

        return ntpath.join(windows.paths().lib_root, agent_providers.CODEX.binary, "bin")

    def prepare(self, *, worktree: str, config_dir: str, project: str) -> None:
        """What must exist on disk before this CLI is exec'd into `worktree` (architecture A12).

        Fast, idempotent steps, each guarded on its own so one failure never costs the others:
          * the repo-hook exposure survey (finding 4) — first, so visibility never depends on another step;
          * git-native hooks (`install_hooks.py`) — the push guard and quick gate (D10);
          * workspace trust (or the TUI waits on a trust modal forever) and an MCP re-assertion when
            `config.toml` lost it (`_converge_account_config`);
          * the subagent catalogue, `$CODEX_HOME/agents/*.toml` (`agent_layer.place_subagents`).
          * the OMX layer — a detached converge when the home has none (`_converge_omx_layer`).
        Nothing raises: a committed dispatch is never unwound by a best-effort write."""
        if not config_dir:
            log.warning("codex prepare: no account config dir for %s — skipping the account-scoped "
                        "steps (workspace trust, MCP registration). This runner will open on a trust "
                        "modal and reach no control plane.", worktree)
        steps: tuple[tuple[str, object], ...] = (
            # Its own step, first: a visibility control must not depend on another step succeeding.
            ("repo hook survey", lambda: self._warn_on_repo_hooks(worktree)),
            ("git hooks", lambda: self._install_git_hooks(worktree)),
        )
        if config_dir:
            steps += (
                ("account config converge",
                 lambda: self._converge_account_config(config_dir=config_dir, worktree=worktree)),
                ("subagent catalogue", lambda: self._place_subagents(config_dir)),
                ("omx layer", lambda: self._converge_omx_layer(config_dir=config_dir)),
            )
        for what, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — see the paragraph above
                log.warning("codex prepare: %s failed for %s (%s: %s) — the dispatch continues without "
                            "it", what, worktree, type(exc).__name__, exc)
        return None

    # ── ACP runtime ────────────────────────────────────────────────────────────────────────────────

    def acp_launch(self, ctx: AcpLaunchContext) -> AcpLaunch | None:
        """`codex-acp` for one run (the constants above), or None — the legacy launch — for an API-key run on a
        static node whose config home is not on this host: its key reaches the engine only by `_login_api_key`
        into that home, never the environment. In a MicroVM guest (`ctx.guest`) the host signs in instead
        (`ACP_KEY_SIGN_IN`): it reads the VM's own account key from the daemon and sends it in ACP `authenticate`,
        and the engine logs it into the guest's config home; no environment holds it there either.

        The control server is the ACP entry every launch carries; codex-acp keeps an account's own registered
        `control` server instead when its config has one (measured, 2.2.2), which forwards the launch's
        `JAAH_CRED_SOCKET`/`JAAH_PROJECT_MEMORY` by name (`register_mcp`) — the ACP entry cannot."""
        if ctx.api_key_account and not ctx.guest and not (ctx.config_dir and os.path.isdir(ctx.config_dir)):
            log.info("codex acp: task %s's API-key account has no config home on this host (%r) — launching "
                     "the legacy runtime", ctx.task_id, ctx.config_dir)
            return None
        harness = self._acp_harness(ctx)
        if harness is None:
            config = dict(_ACP_CONFIG, hooks=_acp_hooks())
        else:
            hooks_file, dispatch = harness
            config = dict(_ACP_CONFIG, bypass_hook_trust=True, projects=_untrusted_roots(ctx),
                          hooks=_acp_harness_hooks(hooks_file, dispatch))
        if ctx.model:
            config["model"] = ctx.model
        env = {
            _ACP_PATH_ENV: agent_providers.CODEX.binary,
            _ACP_CONFIG_ENV: json.dumps(config, sort_keys=True),
            _ACP_MODE_ENV: _ACP_FULL_ACCESS,
            **self.env(config_dir=ctx.config_dir),
        }
        from . import agent_layer

        command, *args = agent_layer.jaah_mcp_command()
        return AcpLaunch(argv=(ACP_BINARY,), env=env, session_meta={},
                         mcp_servers=(control_mcp_server(command, args, {}),),
                         prepare=lambda: self._acp_prepare(ctx, harness),
                         authenticate={**ACP_KEY_SIGN_IN, "account": ctx.api_key_account}
                         if ctx.guest and ctx.api_key_account else None)

    def _acp_harness(self, ctx: AcpLaunchContext) -> tuple[str, dict] | None:
        """`(the run's hook file path, its content)` when the fleet harness runs in this run's engine, else None: a
        MicroVM guest (nothing on this host writes its home), no config home here, a Windows node (the CLI runs hooks
        in PowerShell and the dispatcher's command is a POSIX shell line), or a `codex-acp` that would not keep a
        repo's own hooks out (`_acp_engine_fences_repo_hooks`, said in the log). Raises when the harness is
        unreadable (`base.read_harness`): the guards live there."""
        if ctx.guest or host_platform.is_windows() or not (ctx.config_dir and os.path.isdir(ctx.config_dir)):
            return None
        if not _acp_engine_fences_repo_hooks():
            log.warning("codex acp: task %s — this node's %s is not the patched pin, so the fleet harness and "
                        "oh-my-codex's hooks do NOT run on this run (only the trusted read-only subagent guard)",
                        ctx.task_id, _ACP_PACKAGE)
            return None
        import fleet_launch  # noqa: PLC0415 — in-function: fleet_launch imports this package

        chains = fleet_launch.guard_chains(ctx.worktree, guard=ctx.guard)
        return _run_hooks_path(ctx.config_dir, ctx.session_id), _dispatch_hooks(chains)

    def _acp_prepare(self, ctx: AcpLaunchContext, harness: tuple[str, dict] | None = None) -> None:
        """`prepare`'s steps for an ACP run, each guarded alone; nothing when the worktree is not on this host
        (a MicroVM's context carries guest paths, and its guest is seeded as on the legacy path).

        Beyond `prepare`: the legacy charter carrier (`AGENTS.override.md`) is removed, since the host sends
        the charter as the first prompt and a stale override would hand this run another task's charter; and
        an API-key account's key is logged into its home (`_login_api_key`). With the fleet harness (`harness`):
        the run's hook file is written and the account's `hooks.json` cut to oh-my-codex's entries
        (`_ACP_OMX_ONLY`), each RAISING when it cannot be (`_write_run_hooks`, `_account_hooks_omx_only`). Without
        it, the read-only guard is trusted by hash (`_trust_acp_hooks`). A repo's `.codex/hooks.json` runs on
        neither path (`_untrusted_roots`; untrusted without a bypass), so there is nothing to survey."""
        if not os.path.isdir(ctx.worktree):
            return
        if harness is not None and ctx.config_dir:
            # Not guarded like the steps below: a missing hook file refuses every tool call, and a stale chain
            # left in the account's file would settle beside the host.
            self._write_run_hooks(*harness)
            self._account_hooks_omx_only(config_dir=ctx.config_dir)
        steps: tuple[tuple[str, object], ...] = (("git hooks", lambda: self._install_git_hooks(ctx.worktree)),)
        if ctx.config_dir:
            steps += (
                ("charter override removal", lambda: self._run_as_runner(
                    ["rm", "-f", "--", str(Path(ctx.config_dir) / CHARTER_FILE)], what="charter override removal")),
                ("account config converge",
                 lambda: self._converge_account_config(config_dir=ctx.config_dir, worktree=ctx.worktree)),
                ("subagent catalogue", lambda: self._place_subagents(ctx.config_dir)),
                ("omx layer", lambda: self._converge_omx_layer(config_dir=ctx.config_dir)),
            )
            if harness is None:
                steps += (("read-only guard trust", lambda: self._trust_acp_hooks(config_dir=ctx.config_dir)),)
            if ctx.api_key_account:
                steps += (("api key login",
                           lambda: self._login_api_key(config_dir=ctx.config_dir, slug=ctx.api_key_account)),)
        for what, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — as `prepare`: a dispatch is never unwound by a write
                log.warning("codex acp prepare: %s failed for %s (%s: %s) — the run continues without it",
                            what, ctx.worktree, type(exc).__name__, exc)

    def _trust_acp_hooks(self, *, config_dir: str) -> None:
        """Record `_acp_hooks` as trusted in this account's config.toml (`_HOOK_TRUST`): without it the CLI
        skips the read-only guard, and reader subagents can write."""
        from . import agent_layer

        result = agent_layer.run_provider_cli([runner_env.python_cmd(), "-I", "-c", _HOOK_TRUST,
                                               json.dumps(_acp_hooks())],
                                              env={provider_health.CODEX_HOME_ENV: config_dir})
        if result["exit_code"] != 0 or not result["stdout"].startswith("trusted="):
            log.warning("codex acp prepare: the read-only subagent guard could not be trusted in %s (exit %s: %s) "
                        "— reader subagents on this run are not fenced", config_dir, result["exit_code"],
                        (result["stderr"] or "").strip()[-300:])

    def _write_run_hooks(self, path: str, dispatch: dict) -> None:
        """Write the run's hook file as the account's uid (`_RUN_HOOKS_WRITE`), or RAISE: an engine whose
        dispatcher has no hook file refuses every tool call (`hook_bridge_core._cli_verdict`)."""
        argv = [runner_env.python_cmd(), "-I", "-c", _RUN_HOOKS_WRITE, _AGENTS_DIR, path,
                json.dumps(dispatch, indent=2), _RUN_HOOKS_STEM, str(_RUN_HOOKS_TTL_DAYS)]
        if self._run_as_runner(argv, what="run hook file") is None:
            raise RuntimeError(f"codex acp: the run hook file {path} could not be written — the fleet's guards "
                               "would refuse every tool call")

    def _account_hooks_omx_only(self, *, config_dir: str) -> None:
        """The account's `hooks.json` cut to oh-my-codex's entries (`_HOOKS_MERGE` with `_ACP_OMX_ONLY`), or removed
        when that cannot run: the trust bypass runs this file, and a legacy dispatch's chains in it would settle
        this task a second time. OMX's entries come back on the next `omx setup`."""
        path = str(Path(config_dir) / HOOKS_FILE)
        argv = [runner_env.python_cmd(), "-I", "-c", _HOOKS_MERGE, _OMX_LAYER_DIR, path, _ACP_OMX_ONLY,
                omx_layer.OMX_HOOK_MARKER]
        out = self._run_as_runner(argv, what="hooks merge")
        if out is not None:
            log.info("codex acp prepare: %s: %s", path, out.strip() or "no verdict")
            return
        log.warning("codex acp prepare: %s could not be cut to oh-my-codex's entries, so it is removed — "
                    "oh-my-codex's hooks do not run on this run", path)
        if self._run_as_runner(["rm", "-f", "--", path], what="account hooks removal") is None:
            raise RuntimeError(f"{path} could be neither merged nor removed")

    def _login_api_key(self, *, config_dir: str, slug: str) -> None:
        """`codex login --with-api-key` into this account's own home, the key on STDIN (architecture C11): the
        CLI keeps it in `$CODEX_HOME/auth.json` (0600, the runner's uid) and the engine reads it from there, so
        no process environment ever holds it. Re-run every dispatch, so a rotated key lands. The value is a
        LIVE KEY: never logged; a failure is logged by slug."""
        import cred_broker  # in-function: a daemon module (see the header)
        from . import agent_layer

        env = {provider_health.CODEX_HOME_ENV: config_dir}
        try:
            key = cred_broker.account_api_key(slug)
        except Exception as exc:  # noqa: BLE001 — an unreachable CP says nothing about the key: keep the login
            log.warning("codex acp prepare: fetching API-key account %r's key failed (%s) — the home keeps its "
                        "current login", slug, type(exc).__name__)
            return
        login = [agent_providers.CODEX.binary, *credential_store_args(), "login", "--with-api-key"]
        result = agent_layer.run_provider_cli(login, env=env, input=key) if key else None
        if result is not None and result["exit_code"] == 0:
            return
        # FAIL CLOSED: a key the control plane no longer serves (revoked, deleted) must not live on in `auth.json`
        # from an earlier dispatch, so the home is logged out and the run starts unauthenticated.
        agent_layer.run_provider_cli([agent_providers.CODEX.binary, *credential_store_args(), "logout"], env=env)
        log.warning("codex acp prepare: no key was logged in for API-key account %r (%s) — the home is logged out "
                    "and this run starts unauthenticated", slug,
                    "none served" if result is None else f"login exit {result['exit_code']}")

    # ── Hooks ──────────────────────────────────────────────────────────────────────────────────────

    def render_hooks(
        self, *, settings_path: str, chains: LifecycleChains, config_dir: str = "",
    ) -> tuple[str | None, str]:
        """`(the hooks.json content, the flag that installs it)` — and the flag is ALWAYS EMPTY.

        No `--settings` equivalent (finding 1), so `settings_path` is unused. A non-empty `on_error` is
        logged, never silently dropped (`_EVENTS`)."""
        if not chains.any_set():
            return (None, "")
        if chains.on_error:
            log.warning(
                "codex render_hooks: the fleet composed an on_error chain and this CLI has no event to "
                "run it on (no StopFailure, and Stop does not fire on an API-error turn), so it will NOT "
                "fire for this runner. On a pool slot with a signed-in account the node reports an "
                "API-error turn in its place, off the rollout and the idle pane (`vendor_turn_errors`)."
            )
        hooks = chain_hook_groups(chains, _EVENTS)
        # The question capture (Claude's comes from the harness layer this CLI does not inherit): parks
        # the task and files each question for the console. Only on a dispatch, as the rest.
        hooks.setdefault("PreToolUse", []).append({
            "matcher": agent_providers.CODEX.ask_tool,
            "hooks": [{"type": "command", "command": _ask_capture_command()}],
        })
        # A turn ending in a prose ask ("waiting for your approval") is sent back once.
        hooks.setdefault("Stop", []).append({
            "hooks": [{"type": "command", "command": _ask_nudge_command()}],
        })
        # Codex 0.157.1 does not project an agent TOML's sandbox_mode into the child, and the
        # runner's bypass profile is inherited. The CLI supplies agent_type to PreToolUse, so the
        # hook is the enforcing boundary for reader roles (including OMX-owned native TOMLs).
        hooks["PreToolUse"].append({
            "hooks": [{"type": "command", "command": _readonly_guard_command()}],
        })
        # On Windows the CLI runs hooks in PowerShell, never the Git Bash that launched it (codex-rs
        # `shell_detect::default_user_shell`, 0.163.0).
        return (json.dumps({"hooks": host_hook_commands(hooks, WINDOWS_HOOK_SHELL_POWERSHELL)}, indent=2), "")

    def write_hooks(self, *, settings_path: str, chains: LifecycleChains,
                    mode: int | None = None, config_dir: str = "") -> str:
        """`render_hooks` MERGED into `<config_dir>/hooks.json` — NOT into `settings_path`.

        No account means no hooks, logged loudly. Merged as the account's own uid (`_HOOKS_MERGE`): the
        file is shared with OMX, and 0600 to the daemon. When that merge could not run OR failed
        (`_run_as_runner` answers None either way), the fleet's rendering is written whole by the shared
        `base.write_rendered_hooks` — OMX's entries are lost for this dispatch, said out loud, and return on
        the next `omx setup`. A write refused on that path too RAISES rather than dispatch a runner with no
        lifecycle."""
        content, flag = self.render_hooks(
            settings_path=settings_path, chains=chains, config_dir=config_dir)
        if content is None:
            return flag
        if not config_dir:
            log.warning("codex write_hooks: no account config dir, so this runner gets NO lifecycle "
                        "hooks — it will not claim, heartbeat, arm its autoclose or settle.")
            return flag
        path = str(Path(config_dir) / HOOKS_FILE)
        # `-I`: the script runs at the account's uid with the delegate's cwd first on `sys.path` (a planted
        # `json.py` there would load); isolated mode drops the cwd and every `PYTHON*` variable. The
        # script inserts `_OMX_LAYER_DIR` itself, so it still finds `omx_layer` under `-I`.
        argv = [runner_env.python_cmd(), "-I", "-c", _HOOKS_MERGE, _OMX_LAYER_DIR, path, content,
                omx_layer.OMX_HOOK_MARKER]
        out = self._run_as_runner(argv, what="hooks merge")
        if out is not None:
            log.info("codex write_hooks: %s: %s", path, out.strip() or "no verdict")
            return flag
        log.warning("codex write_hooks: the merge into %s could not run as the account's uid (or "
                    "failed), so the file is being REWRITTEN with the fleet's entries alone — OMX's own "
                    "hooks were not preserved for this dispatch.", path)
        # The caller's `mode` is for `_meta` artifacts; this file lives in the account home and names every
        # hook command, so it is always 0600.
        return write_rendered_hooks(self, settings_path=path, chains=chains, mode=0o600, config_dir=config_dir)

    def register_mcp(self, *, config_dir: str) -> None:
        """Register the control-plane MCP server natively (architecture A9).

        `codex mcp add control <node interpreter> <absolute server.py>` via `agent_layer`; no `scope_args`. Never a marketplace manifest (#19372).
        Then `_assert_mcp_env_passthrough`, because `mcp add` takes no forward-by-name option.

        Measured: the server reaches the model as ONE NAMESPACE (`mcp__control`) with plain tool names; a
        flat `mcp__control__task_get` is rejected. So `agent_providers.CODEX.mcp_tool_prefix` is wrong as
        prose, left for a charter-wording change; the tools work either way."""
        if not config_dir:
            return None
        # `register_control_mcp` logs nothing itself, so the failure is logged here.
        result = self._register_control_mcp(config_dir=config_dir)
        if not (result or {}).get("registered"):
            log.warning("codex register_mcp: registering the control-plane MCP server in %s FAILED "
                        "(exit %s: %s) — a runner on this account cannot settle its own task.",
                        config_dir, (result or {}).get("exit_code"),
                        ((result or {}).get("stderr") or "").strip()[-300:])
            return None
        self._assert_mcp_env_passthrough(config_dir=config_dir)
        return None

    def _assert_mcp_env_passthrough(self, *, config_dir: str) -> None:
        """Make the registered control-plane server inherit `_MCP_ENV_VARS` (`_MCP_ENV_PASSTHROUGH`).

        Without it a runner on a co-located node does its work and then cannot upload or settle: the
        server dials the default socket, which does not exist there. Logs rather than raises."""
        path = str(Path(config_dir) / "config.toml")
        argv = [runner_env.python_cmd(), "-I", "-c", _MCP_ENV_PASSTHROUGH, _AGENTS_DIR, path, _mcp_server_table(),
                json.dumps(list(_MCP_ENV_VARS))]
        out = self._run_as_runner(argv, what="control-plane MCP env passthrough")
        verdict = (out or "").strip()
        if verdict not in ("env=present", "env=patched"):
            log.warning("codex register_mcp: %s does not forward %s to the control-plane MCP server "
                        "(%s) — a runner on a co-located node cannot settle its own task.",
                        path, ", ".join(_MCP_ENV_VARS), verdict or "the patch did not run")

    # ── Session identity + ingest ──────────────────────────────────────────────────────────────────

    def discover_session_id(self, *, config_dir: str, worktree: str, since: float) -> str | None:
        """The id of the session this CLI started after `since` IN `worktree`, or None while it cannot be
        told apart.

        Read off the rollout NAME, and accepted only when (a) mtime is after `since`, (b) the head's own
        `session_meta.session_id` equals the name's uuid (a name/content mismatch would pin an id
        `transcript()` can never resolve — a permanent outage), and (c) `session_meta.cwd` is this tree
        (sessions are bucketed by date, not project). These defend against ACCIDENT only: cwd is self-written,
        so a co-tenant can impersonate; the real boundary is the account (`session_sweep._vendor_transcript`,
        `transcript_root`). Unattributable candidates are skipped. An incomplete walk raises
        `IncompleteRolloutWalk` rather than answering `None`."""
        if not config_dir:
            return None
        root = Path(config_dir) / _SESSIONS_DIR
        want = _real(worktree)
        # Collect cheaply, then probe newest-first under three bounds: entries walked and candidates probed
        # (`_rollout_candidates`), and bytes read (`_HeadBudget`). Attacker-set mtimes cannot buy reads.
        budget = _HeadBudget()
        found: str | None = None
        complete, capped, candidates = _rollout_candidates(root, since=since)
        for _mtime, path in candidates:
            match = _ROLLOUT_ID_RE.search(path.stem)
            if not match:
                continue
            named = match.group(1)
            meta = _rollout_meta(path, budget)
            # Out, not on: later candidates would read nothing and be mis-tallied.
            if budget.exhausted:
                break
            if not meta:
                budget.saw_no_meta(path)
                continue
            # Name and content must agree (see the docstring).
            recorded = meta.get("session_id")
            if not (isinstance(recorded, str) and recorded == named):
                budget.saw_unnamed()
                continue
            if want and _rollout_cwd(meta) != want:
                budget.saw_other_tree()
                continue
            found = named
            break
        budget.report("discover_session_id")
        # A scan-truncated walk may not name a session (the caller pins first-wins; the newest may be the
        # one shed) and may not answer `None` either (`IncompleteRolloutWalk`).
        if not complete:
            raise IncompleteRolloutWalk(
                "the walk of %r was truncated, so the newest rollout of this tree may not be in the "
                "candidate set — refusing to name a session off a knowingly partial one%s"
                % (str(root), " rather than pinning a possibly older run's id" if found else ""))
        # The probe cap and byte budget shed only strictly-older candidates, so a found id stands; only an
        # EMPTY result is ambiguous there.
        if found is None and capped:
            raise IncompleteRolloutWalk(
                "%r holds more than the %d rollouts one call probes and none of the newest %d is "
                "attributable to this run — the answer may be among the ones the cap shed"
                % (str(root), MAX_TRANSCRIPT_PROBES, MAX_TRANSCRIPT_PROBES))
        if found is None and budget.exhausted:
            raise IncompleteRolloutWalk(
                "the %d-byte per-call budget for reading rollout heads ran out under %r before any "
                "candidate was attributed to this run — the remaining candidates were never read"
                % (_META_CALL_BYTES, str(root)))
        return found

    def credential_state(self, *, config_dir: str) -> str:
        """One of `agent_config_dirs.CREDS_TOKEN` / `CREDS_NONE` / `CREDS_UNREADABLE` for this account.

        Unreadable is never collapsed into none (a kill-safety gate would reap a live runner). Answered by
        `credential_file_present` (an `lstat`) of `CodexLoginAdapter.credential_path` without reading it;
        a symlink is not a credential. The `lstat` first is the unreadable leg alone: an untraversable
        `config_dir` (`PermissionError`)."""
        import agent_config_dirs

        from .codex_login import MAX_CREDENTIAL_BYTES, CodexLoginAdapter

        if not config_dir:
            return agent_config_dirs.CREDS_NONE
        path = CodexLoginAdapter().credential_path(config_dir=config_dir)
        import runner_credentials  # noqa: PLC0415 — a daemon module, as `agent_config_dirs`

        if runner_credentials.read_through_runner(path):
            # A Windows NodeLink store: only the runner account sees it.
            return runner_credentials.credential_state(
                path, shape=runner_credentials.SHAPE_FILE, max_bytes=MAX_CREDENTIAL_BYTES)
        try:
            os.lstat(path)
        except PermissionError:
            return agent_config_dirs.CREDS_UNREADABLE
        except OSError:
            return agent_config_dirs.CREDS_NONE
        if credential_file_present(path, max_bytes=MAX_CREDENTIAL_BYTES):
            return agent_config_dirs.CREDS_TOKEN
        return agent_config_dirs.CREDS_NONE

    def transcript(self, *, config_dir: str, worktree: str, session_id: str) -> str | None:
        """This session's rollout file, or None when it is not there (yet, or at all).

        Found by matching the id in the name, never a composed date path. `None` (TERMINAL `unavailable`)
        only after a complete walk; an incomplete one raises `IncompleteRolloutWalk`, but a match found is
        returned regardless (reasoning at the bottom)."""
        if not (config_dir and session_id):
            return None
        # A runner-reported value: must be a uuid (same pattern as the name read) before it names a file.
        if not _ROLLOUT_ID_RE.search(f"-{session_id}"):
            log.warning("codex transcript: %r is not a session id — refusing to glob for it", session_id)
            return None
        root = Path(config_dir) / _SESSIONS_DIR
        # Bounded and newest-first like discovery: the runner chooses how many (hardlinked, free) names
        # match; unbounded, 3,000 took 14.7s per call per tick.
        refused = 0
        found: str | None = None
        # The same per-call byte budget; here the probe cap is reachable by construction.
        budget = _HeadBudget()
        complete, capped, candidates = _rollout_candidates(root, name_suffix=f"-{session_id}")
        for _mtime, path in candidates:
            # CONTENT gate, not name: a runner can hardlink its account's OAuth credential under a rollout
            # name and have it uploaded forever. Only a real rollout records this `session_meta.session_id`.
            recorded = _rollout_session_id(path, budget)
            # A never-read candidate is not a refused one.
            if budget.exhausted:
                break
            if recorded == session_id:
                found = str(path)
                break
            refused += 1
        budget.report("transcript")
        if refused:
            # One line per call, never per candidate (a journal-flood primitive).
            log.warning("codex transcript: %d candidate(s) named for session %r do not record it — "
                        "refusing them", refused, session_id)
        # A match found on a partial walk is kept: it is content-gated, so shedding can only lose a match,
        # never invent one, and busy accounts are permanently over the cap. An EMPTY result on an
        # incomplete walk raises (`IncompleteRolloutWalk`) instead of retiring an on-disk transcript.
        if found is None:
            if not complete:
                raise IncompleteRolloutWalk(
                    "the walk of %r was truncated before the whole tree was seen, so session %r has no "
                    "transcript here because none was REACHED — not because none is on disk"
                    % (str(root), session_id))
            if capped:
                # The cheapest to reach: 256 free hardlinked forgeries push the real rollout off the list.
                raise IncompleteRolloutWalk(
                    "more rollouts under %r are NAMED for session %r than one call probes (%d), and none "
                    "of the newest %d records it — the real one may be among those the cap shed"
                    % (str(root), session_id, MAX_TRANSCRIPT_PROBES, MAX_TRANSCRIPT_PROBES))
            if budget.exhausted:
                raise IncompleteRolloutWalk(
                    "the %d-byte per-call budget for reading rollout heads ran out under %r before any "
                    "candidate was read as session %r — the rest were never opened"
                    % (_META_CALL_BYTES, str(root), session_id))
        return found

    def transcript_root(self, *, config_dir: str) -> str:
        """`<config_dir>/sessions` — this vendor's whole transcript subtree and nothing above it.

        Never the account home, which holds `<config_dir>/auth.json` (`base.AgentAdapter.transcript_root`)."""
        return str(Path(config_dir) / _SESSIONS_DIR)

    def session_members(self, *, config_dir: str, transcript: str) -> list[tuple[str, str]]:
        """This session's SUB-AGENT threads: `[("subagent-<thread id>.jsonl.gz", rollout path)]`.

        A sub-agent is its own rollout in the same date tree (measured, 0.151.0): its first `session_meta`
        records its own `id` and the ROOT thread's `session_id` (depth-2 threads included), with the parent's
        history forked in ahead of its own turns but none of the parent's `token_count` lines — so folding it
        adds exactly the sub-agent's spend. That record is the link; the NAME's uuid must equal `id`, the same
        name/content gate `discover_session_id` applies. Nothing else in the account home is per-session.

        Walked newest-first from the root thread's own start, under the same bounds as discovery; a walk
        that could not complete raises `IncompleteSessionMembers` carrying the threads it DID verify."""
        if not (config_dir and transcript):
            return []
        match = _ROLLOUT_ID_RE.search(Path(transcript).stem)
        if not match:
            return []
        root_id = match.group(1)
        budget = _HeadBudget()
        started = _parse_iso_epoch(_rollout_meta(Path(transcript), budget).get("timestamp"))
        # A sub-agent starts after its root, so the root's own start is a floor (less a clock-skew minute).
        since = started - 60.0 if started else None
        complete, capped, candidates = _rollout_candidates(Path(config_dir) / _SESSIONS_DIR, since=since)
        members: list[tuple[str, str]] = []
        for _mtime, path in candidates:
            named = _ROLLOUT_ID_RE.search(path.stem)
            if not named or named.group(1) == root_id:
                continue
            meta = _rollout_meta(path, budget)
            if budget.exhausted:
                break
            if meta.get("session_id") == root_id and meta.get("id") == named.group(1):
                members.append((f"subagent-{named.group(1)}.jsonl.gz", str(path)))
        budget.report("session_members")
        if not complete or capped or budget.exhausted:
            raise IncompleteSessionMembers(
                "the walk for session %r's sub-agent rollouts could not see every candidate, so the %d "
                "found may not be all of them" % (root_id, len(members)), sorted(members))
        return sorted(members)

    def read_usage(self, line: bytes) -> TokenCounts | None:
        """One rollout line's token spend, or None when that line reports none (which is most lines).

        Carrier (measured, 0.151.0): `{"type": "event_msg", "payload": {"type": "token_count", "info":
        {"total_token_usage": {...}, "last_token_usage": {...}}}}`.

        `last_token_usage`, NEVER the cumulative `total_token_usage` — the fold sums lines. `input` is
        `input_tokens - cached_input_tokens - cache_write_input_tokens` (this vendor's input includes
        both cache classes). `reasoning_output_tokens` is omitted: already inside `output_tokens`.
        `cache_write_input_tokens` uses the provider-neutral first cache-write bucket; its historical `5m`
        name is not a claim that this vendor exposes an Anthropic TTL.

        A `turn_context` line returns a zero-spend reading naming the model (the usage carrier names none)."""
        record = json_object(line)
        if record is None:
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        # The model-naming arm, before the usage read (it carries no `token_count`).
        if record.get("type") == "turn_context":
            model = payload.get("model")
            return (TokenCounts(model=model)
                    if isinstance(model, str) and model else None)
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        if not isinstance(info, dict):
            raise ValueError("Codex token-count carrier has no info object")
        last = info.get("last_token_usage")
        if not isinstance(last, dict):
            # Never fall back to the cumulative half.
            raise ValueError("Codex token-count carrier has no per-turn usage")
        required = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
        if any(
            isinstance(last.get(field), bool) or not isinstance(last.get(field), int)
            or last[field] < 0
            for field in required
        ):
            raise ValueError("Codex per-turn usage has an invalid required token count")
        cache_write_raw = last.get("cache_write_input_tokens", 0)
        if (isinstance(cache_write_raw, bool) or not isinstance(cache_write_raw, int)
                or cache_write_raw < 0):
            raise ValueError("Codex per-turn usage has an invalid cache-write token count")
        # Every field clamped at zero: the CP refuses a negative count, and one hostile line would cost the
        # session its whole capture.
        cached = max(0, count_int(last.get("cached_input_tokens")))
        cache_write = cache_write_raw
        gross_input = max(0, count_int(last.get("input_tokens")))
        # These are documented subsets of gross input. If a vendor/schema change violates that
        # partition, no measured reading is safer than billing more prompt tokens than the vendor did.
        if cached + cache_write > gross_input:
            raise ValueError("Codex cache classes exceed the vendor input total")
        output = max(0, count_int(last.get("output_tokens")))
        # The inclusive-input fold; reasoning is already inside `output_tokens`, so no thoughts ride on top.
        fresh, cached, output = fold_inclusive_counts(gross_input - cache_write, cached, output, 0)
        if gross_input + output != last["total_tokens"]:
            raise ValueError("Codex token components do not close onto the vendor total")
        if not (fresh or output or cached or cache_write):
            return None
        # `""`: the fold carries the last `turn_context` model forward (`session_sweep.Analytics`).
        return TokenCounts(
            model="",
            pricing_tier="@long-context" if gross_input > 272_000 else "",
            input=fresh,
            output=output,
            cache_read=cached,
            cache_write_5m=cache_write,
        )

    def turn_error(self, *, transcript: str) -> TurnError | None:
        """The error the rollout's last turn ended on, or None (measured, codex-cli 0.157.1).

        Carrier: after its own retries Codex ends the turn with `{"type": "event_msg", "payload": {"type":
        "task_complete", "turn_id": …, "error": {"message": "exceeded retry limit, last status: 429 Too
        Many Requests", "codex_error_info": {"response_too_many_failed_attempts": {"http_status_code":
        429}}}}}` and goes idle at its composer, firing neither `Stop` nor anything else. ONLY that closing
        record's `error` counts: a clean turn carries `"error": null` whatever it survived on the way (a
        mid-turn `error` event is a retry the CLI may recover from), a `turn_aborted` is the operator's Esc,
        and a turn still open (`task_started` last) is not over. A turn whose start lies before the tail is
        still judged by its end."""
        last: dict | None = None
        for raw in read_tail_lines(transcript, _TURN_TAIL_BYTES) if transcript else []:
            record = json_object(raw)
            if record is None or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") in ("task_started", "task_complete",
                                                                     "turn_aborted"):
                last = payload
        if last is None or last.get("type") != "task_complete" or not isinstance(last.get("error"), dict):
            return None
        text = _codex_error_text(last["error"])
        if not text:
            return None
        # The turn id names the turn; a record without one is named by its words, never by `""` (which
        # would make every later errored turn of this session look already reported).
        return TurnError(marker=str(last.get("turn_id") or "") or text, text=text)

    # ── prepare()'s four steps ─────────────────────────────────────────────────────────────────────

    def _install_git_hooks(self, worktree: str) -> None:
        """Run the fleet's `install_hooks.py` in `worktree` — step 1 of `prepare`, the D10 floor.

        As the session user (it owns `.git/hooks`); the script targets its CWD. Skipped for a folder slot
        (`is_folder_slot`): it has no hooks dir, and run from here git would find an ancestor repo's."""
        if not worktree or is_folder_slot(worktree):
            return
        script = Path(hub_root()) / "tools" / "install_hooks.py"
        if not script.is_file():
            log.warning("codex prepare: %s is missing, so this runner's git-native pre-commit/pre-push "
                        "hooks were not installed — the quick gate and the push guard will not fire.",
                        script)
            return
        self._run_as_runner([runner_env.python_cmd(), str(script)], cwd=worktree, what="install_hooks")

    def _warn_on_repo_hooks(self, worktree: str) -> None:
        """Say so in the node log when the worktree ships its own hook config.

        Not a refusal or deletion (a repo may legitimately ship hooks); it makes finding 4's exposure visible
        when it goes live."""
        repo_hooks = Path(worktree) / ".codex" / HOOKS_FILE
        try:
            present = repo_hooks.is_file()
        except OSError:
            return
        if present:
            log.warning(
                "codex prepare: %s ships its own hook config and this runner's argv carries "
                "--dangerously-bypass-hook-trust, so those hooks WILL run as this uid, at SessionStart, "
                "before the model reads anything, and in no rollout transcript. Measured; see "
                "agents/codex.py finding 4.", repo_hooks)

    def _converge_account_config(self, *, config_dir: str, worktree: str) -> None:
        """Steps 2 and 3 of `prepare`, in ONE atomic pass over the account's `config.toml`, run AS THE
        ACCOUNT'S OWN UID: record this worktree as a trusted project, and report whether the
        control-plane MCP server is still registered.

        At the owner's uid because: the CLI rewrites this file 0600, so a daemon read fails forever; a
        daemon read-then-rewrite loses concurrent `mcp add` writes; and the trust check must PARSE TOML
        (several spellings of one key) — a duplicate table makes the file unparseable. See `_CONFIG_CONVERGE`."""
        path = str(Path(config_dir) / "config.toml")
        # `-I` for the same reason as `write_hooks`: no cwd on `sys.path`, no `PYTHON*` env, at that uid.
        argv = [runner_env.python_cmd(), "-I", "-c", _CONFIG_CONVERGE, path, os.path.abspath(worktree),
                _mcp_server_table(), json.dumps(list(_MCP_ENV_VARS))]
        out = self._run_as_runner(argv, what="account config converge")
        if out is None:
            log.warning("codex prepare: could not converge %s — this runner may open on a trust modal, "
                        "and its control-plane MCP registration was not checked.", path)
            return
        # Three diagnoses for one symptom (a runner hanging on the trust modal).
        if "trust=unparseable" in out:
            log.warning("codex prepare: %s could not be parsed as TOML, so nothing was appended to it. "
                        "This runner will open on a directory-trust modal and never reach a composer.",
                        path)
        elif "trust=unappendable" in out:
            log.warning("codex prepare: appending a trust record to %s would have made it unparseable, "
                        "so the write was ROLLED BACK and the file is intact. This runner will open on a "
                        "directory-trust modal; the file spells `projects` in a form this cannot extend "
                        "(an inline table, or a non-table value) and needs an operator.", path)
        elif "trust=no-tomllib" in out:
            log.warning("codex prepare: the session user's python has no tomllib (pre-3.11), so %s was "
                        "not read and no trust record was appended. This runner will open on a "
                        "directory-trust modal.", path)
        if "mcp=absent" in out:
            log.warning("codex prepare: %s does not register the control-plane MCP server — "
                        "re-asserting it now, because a runner without it cannot settle its own task.",
                        path)
            self.register_mcp(config_dir=config_dir)
        elif "mcp=stale" in out:
            # Registered, but not forwarding the node's socket: the passthrough alone, no CLI call.
            self._assert_mcp_env_passthrough(config_dir=config_dir)

    def _place_subagents(self, config_dir: str) -> None:
        """The runner's subagents as Codex agent roles: `$CODEX_HOME/agents/<name>.toml`, discovered
        with no config.toml entry and offered as `spawn_agent`'s `agent_type` (measured, 0.151.0)."""
        from . import agent_layer
        from .subagents import CODEX

        agent_layer.place_subagents(dialect=CODEX, dest=str(Path(config_dir) / "agents"))
    def _converge_omx_layer(self, *, config_dir: str) -> None:
        """Step 4 of `prepare`: start a detached OFFLINE converge when this home has no OMX layer.

        `omx_layer.needs_converge` is the one home of the presence question. The converge is the
        account script's own detached form (so this and the first-sight converge cannot drift), run at
        the account's uid, at most once per `CONVERGE_COOLDOWN_SECONDS` per home — it is not awaited,
        so THIS runner launches without the layer either way, and says so. Offline on purpose: `omx
        setup`, the policy and `codex mcp add` are local writes, whereas the online form's `npm
        install -g … @openai/codex@<pin>` would replace the very `codex` this dispatch's `launch.sh`
        is about to exec from that prefix. `omx`'s presence is the postinst arm's job and its refresh
        the deploy sweep's / login finalize's."""
        import provision_accounts  # in-function: a daemon module (see the header)

        if host_platform.is_windows():
            log.info("codex prepare: %s — this runner runs on the charter alone", _OMX_WINDOWS_SKIP)
            return
        reason = omx_layer.needs_converge(config_dir)
        if not reason:
            return
        now = time.time()
        last = _omx_converge_started_at.get(config_dir)
        if last is not None and now - last < provision_accounts.CONVERGE_COOLDOWN_SECONDS:
            log.warning("codex prepare: %s is still missing the OMX layer (%s) and a converge is "
                        "already in flight — this runner launches without it", config_dir, reason)
            return
        _omx_converge_started_at[config_dir] = now
        out = self._run_as_runner(provision_accounts.detached_command(config_dir, offline=True),
                                  what="omx layer converge")
        if out is None:
            # Not started, so not in flight: the next dispatch may try again.
            _omx_converge_started_at.pop(config_dir, None)
            return
        log.warning("codex prepare: %s has no OMX layer (%s) — a detached offline converge was started; "
                    "THIS runner launches without it, so it runs on the charter alone with none of "
                    "OMX's orchestration", config_dir, reason)

    # ── delegation helper ──────────────────────────────────────────────────────────────────────────

    def _run_as_runner(self, argv: list[str], *, cwd: str = "", what: str) -> str | None:
        """Run ARGV as the uid that owns this account, returning its stdout, or None when it did not run.

        The delegate-or-refuse rule is `agent_layer.run_provider_cli`'s alone. Logs rather than raises
        (`prepare`)."""
        from . import agent_layer

        result = agent_layer.run_provider_cli(argv, cwd=cwd)
        if result["exit_code"] != 0:
            log.warning("codex prepare: %s did not run (exit %s: %s)", what, result["exit_code"],
                        (result["stderr"] or "").strip()[-500:])
            return None
        return result["stdout"]


def _codex_error_text(error: dict) -> str:
    """A `task_complete` error rendered as one line for `break_cause.classify`: the CLI's message, then
    its structured `codex_error_info` kind and HTTP status, spelled `HTTP <code>` so the shared patterns
    read the vendor's status rather than guessing from prose."""
    message = error.get("message")
    parts = [message.strip()] if isinstance(message, str) and message.strip() else []
    info = error.get("codex_error_info")
    if isinstance(info, dict):
        for kind, detail in info.items():
            parts.append(f"codex_error_info: {kind}")
            status = detail.get("http_status_code") if isinstance(detail, dict) else None
            if isinstance(status, int) and not isinstance(status, bool):
                parts.append(f"HTTP {status}")
    elif isinstance(info, str) and info:
        parts.append(f"codex_error_info: {info}")
    return " | ".join(parts)


def _real(path: str) -> str:
    """`path` resolved, or "" when it cannot be (resolved so a symlinked pool root still matches).

    `ValueError` is caught: non-strict `realpath` swallows `OSError`, but an embedded NUL from a
    runner-written `session_meta.cwd` raises `ValueError` — one decoy would abort every discovery walk."""
    try:
        return os.path.realpath(path) if path else ""
    except (OSError, ValueError):
        return ""


def _rollout_candidates(
    root: Path, *, since: float | None = None, name_suffix: str = "",
) -> tuple[bool, bool, list[tuple[float, Path]]]:
    """`(the walk saw the whole tree, the probe cap shed candidates, the plausible rollout files under
    `root`, newest first)`, BOUNDED.

    `since` drops pre-launch files; `name_suffix` narrows to one session. Walked by `base.SessionTreeWalk`
    (one `MAX_TRANSCRIPT_SCAN` budget, `lstat` only, single-link regular files), never `glob` (it follows
    symlinked directories and counts only matches, so it bounds nothing). Rollouts sit exactly three
    directory levels down (`YYYY/MM/DD`).

    Two flags because the caps shed differently: the scan cap aborts mid-walk (`newest_first` takes
    directories in DESCENDING name order so the oldest days go first, but it is still only a proxy), so an
    answer found under it cannot be trusted; the probe cap trims a complete mtime-sorted list, so only
    strictly older candidates are shed. Both are REPORTED — a silently trimmed list lets 256 forgeries
    hide the real rollout.
    """
    base_dir = str(root)
    # The root must be a directory in place: the walk never follows a link, so a
    # symlinked `sessions/` would otherwise read as an EMPTY tree (terminal), not as a walk that did not look.
    try:
        if not stat.S_ISDIR(os.lstat(base_dir).st_mode):
            log.warning("codex: %r is not a directory in place — refusing to read it as an empty session "
                        "tree, so this walk's candidate set is PARTIAL", base_dir)
            return (False, False, [])
    except FileNotFoundError:
        # Every account before its first dispatch: a tree that holds nothing, not a truncation.
        return (True, False, [])
    except OSError as exc:
        log.warning("codex: %r cannot be examined (%s) — this walk's candidate set is PARTIAL", base_dir, exc)
        return (False, False, [])

    def _admit(path: str, st: os.stat_result) -> bool:
        if stat.S_ISDIR(st.st_mode):
            return True
        # `root` is level 1; rollout files sit at level 4 and nowhere else.
        if os.path.relpath(path, base_dir).count(os.sep) != _ROLLOUT_DEPTH - 1:
            return False
        name = os.path.basename(path)
        if not (name.startswith("rollout-") and name.endswith(".jsonl")):
            return False
        if name_suffix and not name[:-len(".jsonl")].endswith(name_suffix):
            return False
        return since is None or st.st_mtime >= since

    walk = SessionTreeWalk(base_dir, admit=_admit, newest_first=True)
    candidates = [(st.st_mtime, Path(path)) for path, st in walk.stats(base_dir, max_depth=_ROLLOUT_DEPTH)]
    complete = not walk.shortfall
    if not complete:
        # `%r`: a runner chooses these names.
        log.warning("codex: the walk of %r stopped short — this walk's candidate set is PARTIAL, so a "
                    "transcript that is on disk may not be found: %s", base_dir, walk.shortfall[0])
    # Newest first, so the probe cap sheds the least likely answers.
    candidates.sort(key=lambda item: item[0], reverse=True)
    # Reported, not just applied (see the docstring).
    capped = len(candidates) > MAX_TRANSCRIPT_PROBES
    if capped:
        log.warning("codex: %r yielded %d candidate rollouts and only the newest %d are probed — this "
                    "candidate set is PARTIAL, so a transcript that is on disk may not be found",
                    str(root), len(candidates), MAX_TRANSCRIPT_PROBES)
    return (complete, capped, candidates[:MAX_TRANSCRIPT_PROBES])


class _HeadBudget:
    """ONE CALL'S allowance of rollout-head bytes, and one call's tallies of what those heads said.

    Shared across a call's candidates (`_META_CALL_BYTES`), since a per-candidate bound does not hold.
    Tallies are per call too, so `report()` logs once instead of once per candidate.
    """

    __slots__ = ("remaining", "exhausted", "no_meta", "no_meta_example", "other_tree", "unnamed")

    def __init__(self, total: int = _META_CALL_BYTES) -> None:
        self.remaining = total
        self.exhausted = False
        self.no_meta = 0
        self.no_meta_example = ""
        self.other_tree = 0
        self.unnamed = 0

    def may_read(self) -> bool:
        """Is there anything left to spend? Latches `exhausted`, which every loop breaks on."""
        if self.remaining > 0:
            return True
        self.exhausted = True
        return False

    def spend(self, used: int) -> None:
        """Charge what a head read actually returned (a real 21 KB header costs 21 KB), not its allowance."""
        self.remaining -= used

    def saw_no_meta(self, path: Path) -> None:
        """This rollout's whole scanned head held no `session_meta`. Counted, and the FIRST one kept as
        the example `report()` names."""
        self.no_meta += 1
        if not self.no_meta_example:
            self.no_meta_example = str(path)

    def saw_other_tree(self) -> None:
        """This rollout has a `session_meta` and it names no cwd this call can use."""
        self.other_tree += 1

    def saw_unnamed(self) -> None:
        """This rollout's NAME and its own `session_meta.session_id` disagree (`discover_session_id`)."""
        self.unnamed += 1

    def report(self, what: str) -> None:
        """Say once, at the end of the call, what the probes came to.

        "Another tree" is ordinary (debug). "No `session_meta` at all" can fail every rollout at once (a CLI
        change, or a charter-grown header past `_META_LINE_BYTES`), so it is INFO with an example path —
        `%r`, never `%s`: a runner-chosen name with a newline forges a journal record."""
        if self.no_meta:
            log.info("codex %s: %d probed rollout(s) hold no readable session_meta in their first %d "
                     "records (bounded at %d bytes a line; first: %r) — they cannot be attributed to a "
                     "tree, and if this is every rollout then no session of this vendor will resolve a "
                     "transcript",
                     what, self.no_meta, _META_HEAD_LINES, _META_LINE_BYTES, self.no_meta_example)
        if self.other_tree:
            log.debug("codex %s: %d probed rollout(s) name no session cwd in their first %d records — "
                      "not attributing them to a tree", what, self.other_tree, _META_HEAD_LINES)
        if self.unnamed:
            log.warning("codex %s: %d probed rollout(s) record a session id that is not the one in their "
                        "own file NAME — refusing them, because an id discovered off a name the file "
                        "itself disowns is one no transcript lookup can ever resolve and the caller pins "
                        "it permanently", what, self.unnamed)
        if self.exhausted:
            log.warning("codex %s: the %d-byte per-call budget for reading rollout heads ran out, so the "
                        "remaining candidates were not read and this session's transcript may not be "
                        "found. That is a bound holding, not a bug: an account whose rollouts are large "
                        "enough to reach it either grew a charter past every expectation or is being "
                        "padded on purpose.", what, _META_CALL_BYTES)


def _rollout_meta(path: Path, budget: _HeadBudget) -> dict:
    """This rollout's `session_meta` payload, or `{}` when it has none this reader can trust.

    The single home of that read (both `cwd` and `session_id` gates use it). Via `base.read_head_lines`
    (FIFO-safe). One line first — `session_meta` is the first record on 0.151.0 — then the full head only
    on a miss. `RecursionError` is caught with parse errors. `{}` means "cannot be attributed: skip", and
    is also the exhausted-budget answer, so callers check `budget.exhausted` first."""
    for want_lines in (1, _META_HEAD_LINES):
        if not budget.may_read():
            return {}
        head = read_head_lines(str(path), _META_LINE_BYTES, want_lines)
        budget.spend(sum(len(raw) for raw in head))
        for raw in head:
            record = json_object(raw)
            if record is None or record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            return payload if isinstance(payload, dict) else {}
        if not head:
            break
    return {}


def _rollout_session_id(path: Path, budget: _HeadBudget) -> str:
    """The session id this rollout RECORDS, or "" — the content gate `transcript()` gets its safety from.

    Not the runner-choosable NAME (`CodexAdapter.transcript`). "" when missing, malformed, or budget-starved."""
    recorded = _rollout_meta(path, budget).get("session_id")
    return recorded if isinstance(recorded, str) else ""


def _rollout_cwd(meta: dict) -> str:
    """The resolved cwd a rollout's already-read `session_meta` records, or "" when it says none usable.

    Takes the already-read record so one head read serves both gates. Separates benign confusion only;
    it is self-written, so no fence against a hostile co-tenant. A NUL is refused here (`_real` is the
    backstop)."""
    cwd = meta.get("cwd")
    if isinstance(cwd, str) and cwd and "\x00" not in cwd:
        return _real(cwd)
    return ""


def _parse_iso_epoch(value: object) -> float:
    """A `session_meta.timestamp` (`2026-09-26T13:55:14.123Z`) as epoch seconds, or 0.0 when unusable."""
    if not isinstance(value, str) or not value:
        return 0.0
    from datetime import datetime  # in-function: the one reader of a timestamp in this module
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return stamp.timestamp() if stamp.tzinfo else 0.0


__all__ = ["CodexAdapter", "IncompleteRolloutWalk", "CHARTER_FILE", "HOOKS_FILE"]
