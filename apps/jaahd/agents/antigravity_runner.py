"""agents/antigravity_runner.py — the Google Antigravity `AgentAdapter`: a fleet runner on `agy`.

`agents/base.py` declares what a runner CLI may differ about; this file answers it for `agy` and
`agents/__init__.py` registers it. `agents/antigravity.py` is the same vendor's LOGIN adapter;
`agents/antigravity_keyring.py` is the one home of how an `agy` process sees its account (both use it);
`agents/antigravity_transcript.py` owns every session/transcript answer (this file only delegates).

MEASURED against agy 1.2.11 (linux amd64): its `--help`, the customization guides it ships inside its
own binary (`builtin/skills/agy-customizations/docs/`, extracted to `$HOME/.gemini/antigravity-cli/`
on first run), its changelog (`agy changelog`), the protobuf descriptors in the binary, `agy plugin
validate` / `agy agents` in scratch homes, and scratch-HOME runs of `agy mcp add`; the ACP server against
the Python source its pinned `agy_acp_server` 1.4.1 ships. NOT observed in a signed-in session:
no hook, MCP call or tool name below has been seen FIRING — except the terminal's question picker, its
`ask_question` PreToolUse payload and its TUI (`_TUI`), measured live on a signed-in terminal. Everything that needed that observation is
wired on the vendor's own contract and named in the catalogue's `not-yet-observed` gap, or published as a
degradation (`agent_providers.ANTIGRAVITY`), never claimed.

Structural differences from Claude, each a method below rather than a flag:
  1. `HOME` IS THE ACCOUNT (`antigravity_keyring.account_env`): the CLI has no config-root variable, so
     the whole process — and every shell it runs — sees the account dir as its home.
  2. Hooks come from ONE file, `$HOME/.gemini/config/hooks.json` (the CLI's changelog: "the shared
     `~/.gemini/config/hooks.json`"), shared by every runner on the account. So that file is a fixed
     dispatcher and each dispatch's own hooks live in the file `$JAAH_HOOKS_FILE` names
     (`base.harness_dispatch` renders it from this module's declarations; the Antigravity hook bridge reads
     it through the neutral `hook_bridge_core.py::dispatch`).
  3. No append-system-prompt flag, and a rule file (`GEMINI.md`/`AGENTS.md`) is cut at 24,000 bytes
     (the rules guide) — the charter is several times that. So on the terminal the charter is handed over at
     agy's first `SessionStart` as `systemMessage` steps (the bridge's CHARTER) and the first prompt (`-i`,
     `--prompt-interactive`) carries the task alone; a launch with no hooks wired puts both in that prompt.
  4. `SessionStart` fires once, at the first prompt (measured, agy 1.2.11): on the terminal the session-start WORK
     runs in `launch.sh` before the CLI (`--session-start`), once by construction, and only its CONTEXT (with the
     charter) is handed over at agy's `SessionStart` (the bridge's CONTEXT). The ACP server ignores it, so there the
     host runs it and every other `AcpDialect.host_lifecycle` event (`acp_hook_files`, `acp/lifecycle.py`).
  5. Every account `hooks.json` command runs under `sh -c` on the terminal and by plain exec on the ACP server
     (`shlex.split`, no shell), so each is spelled to work under both (`_bridge_command`).

THE HOOK PARITY TABLE (Claude event/tool -> agy event/tool; "-" = none, see the named degradation):

  Claude                       agy                                     fleet use
  SessionStart                 launch.sh before the CLI (once), its     runner-status start, install_hooks,
                               text + the charter at SessionStart      context router, the guard brief, memory
                               (ACP: the host, joined to the system
                               prompt)
  UserPromptSubmit             first PreInvocation after a Stop (the   console_resume, prompt routing
                               bridge's derivation; ACP: the host)
  Stop                         Stop (`decision: continue`; ACP: the    heartbeat, autoclose arm, quick gate
                               host, at each turn end)
  SessionEnd                   launch.sh once the CLI exited (ACP:     console_exit (`_QUIT`'s pane marker)
                               the host)
  StopFailure                  -                                       the node's turn-error sensor
  PreCompact                   UserPromptSubmit, each turn boundary    read-once reset
                               (ACP: the host, on a `usage_update`
                               drop, `acp/events.py::compaction_observed`)
  PreToolUse Bash              PreToolUse run_command                  push/PR, tmux, stash, dangerous-rm
  PreToolUse Edit|Write        PreToolUse code_action|write_to_file|…  context router (text: next
                                                                       PreInvocation)
  PreToolUse Read|Bash         PreToolUse view_file|run_command        read-once (turns: agy's USER_INPUT)
  PreToolUse AskUserQuestion   PreToolUse ask_question                 question capture (terminal: one
                                                                       round per question, answered in
                                                                       the picker's Write-in row; ACP:
                                                                       the host files the round)
  PreToolUse ScheduleWakeup    -                                       no such tool
  PreToolUse <control>_task_complete  PreToolUse mcp_control_task_complete  settle guard (`call_mcp_tool`
                                                                       unwrapped by the bridge)
  PostToolUse Edit|Write       PostToolUse (same matcher)              context check (text: next
                                                                       PreInvocation)
  (hook context)               PreInvocation `injectSteps`             every queued `additionalContext`
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import secrets

import agent_providers

from . import antigravity_acp_auth, antigravity_keyring, antigravity_transcript
from .antigravity import AntigravityLoginAdapter
from .base import (ACP_CHAIN_EVENTS, ACP_HARNESS_EVENTS, CLAUDE_ONLY_MATCHERS, TASK_ARGV_FLAG, AcpLaunch,
                   AcpLaunchContext, AdapterDefaults, ComposerTui, LifecycleChains, QuitSignature, TokenCounts,
                   TurnError, adapter_env, control_mcp_server, hardened_python, harness_dispatch, harness_entries,
                   tool_path, write_rendered_hooks)
from .base import acp_hook_files as base_acp_hook_files

log = logging.getLogger(__name__)

# ── The vendor's own launch vocabulary (agy 1.2.11 `--help`) ───────────────────────────────────────

# `--dangerously-skip-permissions` ("Auto-approve all tool permission requests without prompting") rides
# EVERY launch: without it the runner stops at its first tool call waiting for a human. `--model`, the
# flag `base_argv` appends, is from the same `--help`.
_BASE: tuple[str, ...] = (agent_providers.ANTIGRAVITY.binary, "--dangerously-skip-permissions")

# The account-level hook dispatcher, relative to the account dir (= the CLI's `$HOME`).
HOOKS_JSON = os.path.join(".gemini", "config", "hooks.json")

# An API-key launch (`api_key_prepare`): the variable, the settings file under `$HOME` and the value that
# makes the CLI read it.
_API_KEY_ENV = "GEMINI_API_KEY"
_API_KEY_SETTINGS = os.path.join(".gemini", "antigravity-cli", "settings.json")
_API_KEY_PROVIDER = "gemini"

# The CLI's first-run wizard (a colour-scheme picker, then Terms of Service & Data Use) and the file its
# Done writes, relative to `$HOME` (measured on agy 1.2.11 in a scratch home). A home without
# it draws the wizard OVER the running session on every launch — a sign-in stores no such record, so an
# OAuth account meets it too — and the whole runner pane is the picker. The keys the CLI writes on Done;
# `enterpriseOnboardingComplete` is left to the CLI.
_ONBOARDING_JSON = os.path.join(".gemini", "antigravity-cli", "cache", "onboarding.json")
_ONBOARDING_DONE = {"consumerOnboardingComplete": True, "onboardingComplete": True}

# The variables naming this dispatch's own hook file; the bridge reads them. One fact each, the neutral
# dispatcher's (`hook_bridge_core.py::HOOKS_FILE_ENV`) and agy's one-release alias (`LEGACY_HOOKS_FILE_ENV`),
# which an older bridge still reads.
DISPATCH_HOOKS_FILE_ENV = "JAAH_HOOKS_FILE"
HOOKS_FILE_ENV = "JAAH_AGY_HOOKS"
# The runner charter file the bridge hands over at agy's `SessionStart` (its `CHARTER_FILE_ENV`).
CHARTER_FILE_ENV = "JAAH_CHARTER_FILE"

# The global custom-agents dir under `$HOME` (agy 1.2.11 `agy agents` lists `<name>.md` there), where the fleet's
# subagent catalogue lands.
AGENTS_DIR = os.path.join(".gemini", "config", "agents")

# ── The ACP runtime: Google's `agy_acp_server` (the `[agy-acp-server]` pin), measured at 1.4.1 ──

# `--uid=`: run as the invoking user. The server defaults to switching to `nobody` when root; the registry
# manifest passes it too, and as the non-root runner it is a no-op (measured).
ACP_ARGV: tuple[str, ...] = (agent_providers.ANTIGRAVITY.acp_binary, "--uid=")
# The server's state root, which it reads as `$GEMINI_HOME` (`paths.py` in the server): the CLI's own `.gemini`
# under the account home, so `config/hooks.json` is the account dispatcher both runtimes share.
_GEMINI_HOME_ENV = "GEMINI_HOME"
# The API-key sign-in: `authenticate` with this method, the key in `_meta["api-key"]`; measured to leave the key
# off disk (`antigravity-acp/settings.json` records only `auth.type`).
ACP_KEY_SIGN_IN = {"method_id": "gemini-api-key", "key_meta": "api-key"}
# `_meta.antigravity.eagerMcpServers`: the server otherwise hides an MCP server's tools behind its own
# `call_mcp_tool` and on-disk schema files; naming the control server there makes its tools the model's own.
_EAGER_MCP_META = "eagerMcpServers"
# A run's hook file older than this many days is removed at the next launch on the account (one per session).
_HOOK_FILE_TTL_DAYS = 30

# The largest first prompt put on argv. Linux refuses one argument over MAX_ARG_STRLEN (131072 bytes)
# with E2BIG and the launch would die; past this the charter is handed over as a file instead.
_INLINE_PROMPT_MAX_BYTES = 120_000

# agy has no SessionEnd event: `launch.sh` runs the session-end chain itself once the CLI exits (`exec_line`),
# passing `reason` as the payload's ending — Jaah's value, since the CLI names none. MEASURED on agy 1.2.11
# (`tests/fixtures/agy_quit/`): `/exit` (alias `/quit`) and Ctrl-C twice end the CLI with status 0, keep its last
# frame and print `Resume with -c (or command below):` then `agy --conversation=<id>` under it. A signal to the CLI
# alone draws the same lines, but a node kill takes `launch.sh` too, so nothing runs; a CLI that dies without
# that line (a crash, a session with no conversation) does not complete the chat. Known limit: the runner's own
# shell can signal its CLI and so end its chat this way — no more than it can already do with `jaah-call
# session.close`, which the socket serves to any process in the pane.
_QUIT = QuitSignature(reason="cli-exited", pane_marker="Resume with -c (or command below):")

# The TUI's vocabulary, MEASURED off a live agy 1.2.11 terminal pane signed in on dev (the recorded
# panes are `tests/fixtures/agy_question_picker/`). `answer_delivery` owns the vendor-neutral mechanism.
_TUI = ComposerTui(
    # The footer under the idle composer (`> ` + `? for shortcuts … <model> · <effort>`).
    ready_marker="? for shortcuts",
    # The same footer while a turn is in flight — the question dialog included.
    busy_marker="esc to cancel",
    # A large paste's placeholder was not measured: never matches, so only the payload's own text proves a paste.
    paste_placeholder=re.compile(r"(?!)"),
    # Every picker footer ends so (`↑/↓ Navigate · enter Select · esc Skip`; multi-select `… enter Submit All ·
    # esc Skip`); the open free-text field's footer is `enter Submit · esc Back` instead.
    dialog_marker="\u00b7 esc Skip",
    # The last row of every picker, `  4. Write-in...` (`> 4. Write-in...` when highlighted): its digit opens the
    # field at once, single- and multi-select alike (`Up` does not wrap to it, so the digit is the key).
    write_in_row=re.compile(r"^\s*(?:>\s+)?([1-9])\. Write-in\.\.\.\s*$"),
    write_in_prompt="Your answer:",
)

# ── The hook translation table ─────────────────────────────────────────────────────────────────────
# The shared rows are `base`'s (`ACP_CHAIN_EVENTS`, `ACP_HARNESS_EVENTS`, `CLAUDE_ONLY_MATCHERS`); only agy's own
# deltas are spelled here. "session_start" is the launch.sh pre-exec, not a hook (module docstring, 4).
# "UserPromptSubmit" and "SessionEnd" are the bridge's DERIVED slots (agy 1.2.11 has neither event): the first
# `PreInvocation` after a `Stop`, and `launch.sh` once the CLI has exited (the bridge's
# LIFECYCLE). Absent: `on_error` — the node's turn-error sensor serves it.

# Claude Code's harness events -> agy's slot. None = agy has no such moment; the row is dropped and its
# loss is a published degradation (module docstring). An event missing from this table RAISES.
_HARNESS_EVENTS: dict[str, str | None] = {
    **ACP_HARNESS_EVENTS,
    # The read-once reset, at every turn boundary: agy 1.2.11 reports no compaction and appends its transcript
    # (measured), so a mid-turn compaction costs one ranged read.
    "PreCompact": "UserPromptSubmit",
}

# Claude's tool matchers -> agy's, as FULL-match regexes over agy's tool name (the bridge matches; agy's
# own matcher is "" = every tool). None = dropped (see the module docstring for each). An unlisted
# matcher RAISES: a Claude name passed through would match nothing and look installed.
_HARNESS_MATCHERS: dict[str, str | None] = {
    # The CLI's names, then the ACP server's SDK names (`edit_file`, `create_file`; agy_acp_server 1.4.1).
    "Edit|Write|MultiEdit": ("code_action|write_to_file|replace_file_content|multi_replace_file_content|"
                             "edit_file|create_file"),
    "Bash": "run_command",
    # The read-once guard: a whole-file `view_file`, or a `cat` through `run_command`.
    "Read|Bash": "view_file|run_command",
    # The project-memory read path (`base.MEMORY_HOOK_PREFIX`): the CLI's read and search tools, then the server's.
    "Read|Grep|Glob": "view_file|grep_search|find|search_directory|find_file",
    # The question capture hook; the bridge respells the call's questions.
    "AskUserQuestion": "ask_question",
    # `mcp_<server>_<tool>`, one underscore (the binary's own `mcp_chrome_devtools_…`), so not
    # `base.CONTROL_TOOL_MATCHERS`: the bridge unwraps a `call_mcp_tool` call to the same name and respells it
    # for the settle guard (`claude_tool_name`). `task_settle` is the pre-rename name (`agent_vocabulary.ALIASES`).
    "mcp__plugin_jaah_control__task_complete": "mcp_control_task_complete",
    "mcp__plugin_jaah_control__task_settle": "mcp_control_task_settle",
    **CLAUDE_ONLY_MATCHERS,
}

def _hooks_file_env(path: str) -> dict[str, str]:
    """The hook file's path under every name a bridge reads: the neutral one, then agy's alias."""
    return {DISPATCH_HOOKS_FILE_ENV: path, HOOKS_FILE_ENV: path}


def bridge_path() -> str:
    """`<hub_root>/tools/antigravity_hook_bridge.py`."""
    return tool_path("antigravity_hook_bridge.py")


def _bridge_command(*extra: str) -> str:
    """The bridge's own invocation, hardened (`base.hardened_python`), behind `env`.

    `env` because the ACP server does not run a hook through a shell: it `shlex.split`s the command and execs
    the first word (agy_acp_server 1.4.1 `acp_server/hooks.py`). A bare `LD_PRELOAD= …` prefix would be that
    first word — an exec failure the server reads as a refusal of EVERY tool call. `env NAME= … python3` sets
    the same empty names under exec and under the terminal CLI's `sh -c` alike."""
    return f"env {hardened_python(bridge_path(), *extra)}"


# This adapter's hook declarations, rendered by the neutral `base.harness_entries`/`base.harness_dispatch`.
translate_harness = functools.partial(harness_entries, vendor=agent_providers.ANTIGRAVITY.value,
                                      events=_HARNESS_EVENTS, matchers=_HARNESS_MATCHERS)
dispatch_hooks = functools.partial(harness_dispatch, vendor=agent_providers.ANTIGRAVITY.value,
                                   chain_events=ACP_CHAIN_EVENTS, events=_HARNESS_EVENTS, matchers=_HARNESS_MATCHERS)


def acp_hook_files(chains: LifecycleChains) -> tuple[dict, dict]:
    """An ACP run's two hook files (`base.acp_hook_files`): the engine's and the host's own."""
    return base_acp_hook_files(chains, vendor=agent_providers.ANTIGRAVITY.value, dispatch=dispatch_hooks)


# The account `hooks.json`'s per-event budgets, in seconds. FIXED, never summed per dispatch: every runner
# on the account reads the one file, so it must not change with whichever dispatch wrote it last. Each tool and
# turn-end budget is above the harness's largest wrapped budget (the 60s quick gate) plus the bridge's own
# headroom; the context events only read a file the bridge wrote.
_ACCOUNT_BUDGETS: dict[str, int] = {"PreToolUse": 300, "PostToolUse": 120, "Stop": 300,
                                    "SessionStart": 30, "PreInvocation": 30}
# Only the tool events take a matcher group; the rest are FLAT handler lists, and a grouped `SessionStart`
# makes agy 1.2.11 skip the WHOLE file ("command hook must specify 'command'", `plugin validate`).
_GROUPED_EVENTS = frozenset({"PreToolUse", "PostToolUse"})

# The hook name's fixed part; the rest is a nonce (see `account_hooks_json`).
_HOOK_NAME_PREFIX = "jaah-fleet-"


def _account_hook_spec() -> dict:
    """The account `hooks.json`'s handlers: one per agy event the bridge serves, each running the bridge.

    NOTHING PER-DISPATCH: the bridge reads each dispatch's own entries through `$JAAH_AGY_HOOKS`, so two
    runners on one account share this file safely. Tool events are GROUPED (matcher + hooks), `""` = every
    tool — the bridge does the matching; every other event is a flat list (`_GROUPED_EVENTS`). Only `command`
    handlers: one entry of a type agy does not take (`http`) makes it skip the whole file."""
    spec: dict = {}
    for event, budget in _ACCOUNT_BUDGETS.items():
        handler = {"type": "command", "command": _bridge_command("--event", event), "timeout": budget}
        spec[event] = [{"matcher": "", "hooks": [handler]}] if event in _GROUPED_EVENTS else [handler]
    return spec


def account_hooks_json(existing: str = "") -> str | None:
    """The account's `hooks.json`, or None when `existing` already IS it (nothing to write).

    The hook NAME is a nonce, so a repository's `.agents/hooks.json` cannot switch it off by name with
    `"enabled": false`; it is KEPT from `existing` when that carries this spec under one, so re-dispatching
    never rewrites a file another runner on the account is reading."""
    spec = _account_hook_spec()
    try:
        current = json.loads(existing) if existing else None
    except (ValueError, RecursionError):
        current = None
    if isinstance(current, dict) and len(current) == 1:
        (name, value), = current.items()
        if isinstance(name, str) and name.startswith(_HOOK_NAME_PREFIX) and value == spec:
            return None
    return json.dumps({f"{_HOOK_NAME_PREFIX}{secrets.token_hex(8)}": spec}, indent=2)


def _guest_account_hooks_lines(path: str, delimiter: str, refuse: str) -> tuple[str, ...]:
    """The ACP script's lines that put the account `hooks.json` at `path` inside a MicroVM guest, where the host
    writes nothing (`AntigravityAdapter.acp_launch`): an exclusive create after `rm`, as the run's hook file."""
    from fleet_launch import shq  # in-function: see the module header

    return (
        f'mkdir -p -- {shq(os.path.dirname(path))} && rm -f -- {shq(path)} && '
        f'( umask 077; set -C; cat > {shq(path)} ) <<\'{delimiter}\' || '
        f"{refuse}'the account hooks.json could not be written' >&2; exit 1; }}",
        account_hooks_json() or "",
        delimiter,
    )


def _write_account_hooks(path: str) -> str:
    """Put the account `hooks.json` in place as the session user — "" or an error.

    Skipped when the file already holds it; otherwise replaced atomically
    (`session_user.replace_file_as_session_user`), so agy never reads a half-written file (a truncated one
    would leave that runner's guards uninstalled)."""
    import session_user

    from .base import read_bounded

    content = account_hooks_json(read_bounded(path, 1 << 20).decode("utf-8", "replace"))
    if content is None:
        return ""
    return session_user.replace_file_as_session_user(path, content)


class AntigravityAdapter(AdapterDefaults):
    """The `agy` CLI. Stateless — the registry holds one instance."""

    provider = agent_providers.ANTIGRAVITY.value
    # `inherits_harness_hooks` True: `render_hooks` TRANSLATES the harness into the dispatch's hook file.
    # `account_layer()` "none": `agy` imports Claude plugins it finds (`agy plugin import claude`).

    # ── Launch ─────────────────────────────────────────────────────────────────────────────────────

    base_command = _BASE
    #: A flag with a value — `-i <prompt>`; see `exec_line`.
    task_delivery_kind = TASK_ARGV_FLAG
    #: `_TUI`, the measured screen. The task rides the argv; a question's answer is typed into the picker's
    #: Write-in row, and an operator message pastes into the idle composer (`answer_delivery`).
    tui_vocabulary = _TUI
    #: `GEMINI_API_KEY`, read once `api_key_prepare` has set `modelProvider`.
    api_key_variable = _API_KEY_ENV
    #: `_QUIT` — inert: no SessionEnd event, so `console_exit` is never installed.
    quit_sig = _QUIT
    #: `register_mcp` re-asserts the control-plane server in this account's
    #: `$HOME/.gemini/config/mcp_config.json` via `agy mcp add control python3 <server.py>` (measured: agy
    #: 1.2.11 writes that file, user level, no scope flag).
    mcp_binary = agent_providers.ANTIGRAVITY.binary
    #: `credential_state` reads the account's token file through this adapter's shape check.
    login_adapter = AntigravityLoginAdapter

    def exec_line(
        self, *, sys_file: str, task_file: str, settings_flag: str,
        model_args: tuple[str, ...] = (), session_args: tuple[str, ...] = (), rc_flag: str = "",
    ) -> list[str]:
        """agy's whole `exec` block for a dispatch's `launch.sh`.

        1. `export JAAH_AGY_HOOKS=<this dispatch's hook file>` (`settings_flag`, composed by
           `render_hooks`, NOT re-quoted): the account's `hooks.json` dispatches through it.
        2. The session-start commands, once, before agy exists (agy has no SessionStart event); a
           failure there is logged by the bridge and never stops the launch.
        3. With hooks wired, the charter is the CLI's built-in instructions: its path is exported as
           `$JAAH_CHARTER_FILE` and the bridge hands it over at agy's first `SessionStart`
           (the bridge's CHARTER), so the first prompt is the task alone. Without hooks
           there is no bridge, and the charter rides the first prompt ahead of the task as before.
        4. The first prompt as ONE `-i` argument when it fits on argv (`_INLINE_PROMPT_MAX_BYTES`); past it,
           an instruction to read the file instead (charter first, then task). A file handed over this way has
           its directory added to the workspace (`--add-dir`). `-i` (`--prompt-interactive`) runs the prompt and
           keeps the session interactive.
        5. The worktree (launch.sh's cwd) is ALWAYS added to the workspace (`--add-dir "$PWD"`): without it
           agy 1.2.11 records an empty `workspace_uris` for the session, and transcript discovery, which binds
           a session to its worktree by that column (`antigravity_transcript.discover_session_id`), finds
           nothing (measured live on dev).
        6. With hooks wired, agy is not `exec`ed: once it exits, the session-end chain runs
           (`--session-end`, the quit signature's reason; agy has no SessionEnd event), silently so the pane still
           shows what the CLI left, and `launch.sh` exits with agy's status. It runs under job control on a
           terminal, as the pane's foreground group, so the pane reads as agy rather than a shell.

        Every interpolated value is quoted, `model_args` included (`projects.default_model` is free-form)."""
        from fleet_launch import shq  # in-function: fleet_launch imports this package

        quoted = " ".join(shq(a) for a in (*model_args, *session_args, *((rc_flag,) if rc_flag else ())))
        argv = " ".join(self.base_argv(model=""))
        lines: list[str] = []
        if settings_flag:
            lines += [f"export {settings_flag}", f"{_bridge_command('--session-start')} </dev/null || true"]
        task_only = (
            'agy_task_n=$(wc -c < "$agy_task")',
            f'if [ "$agy_task_n" -le {_INLINE_PROMPT_MAX_BYTES} ]; then',
            '  agy_prompt="$(cat "$agy_task")"; set --',
            "else",
            "  agy_prompt=\"$(printf 'Your task did not fit in this prompt. Before anything else, read the WHOLE task "
            "file at the path below with your file-viewing tool; it is your task.\\n\\nTask: %s\\n' \"$agy_task\")\"; "
            'set -- --add-dir "$(dirname -- "$agy_task")"',
            "fi",
            'set -- --add-dir "$PWD" "$@"',
            # Job control, so agy is the terminal's foreground group and tmux names the pane `agy`, not `bash`:
            # answer and message delivery refuse a pane whose foreground command is a shell.
            "[ -t 0 ] && set -m",
            f'{argv} {quoted} "$@" -i "$agy_prompt"',
            "agy_rc=$?",
            "set +m",
            f"{_bridge_command('--session-end', _QUIT.reason)} </dev/null >/dev/null 2>&1 || true",
            'exit "$agy_rc"',
        )
        charter_pointer = (
            "Your operating charter did not fit in this prompt. Before anything else, read the WHOLE file "
            "at the path below with your file-viewing tool, every part of it, and follow it: it outranks "
            "everything else, this prompt included.\\n\\nCharter: %s\\n\\n---\\n\\n")
        task_pointer = (
            "Your task did not fit in this prompt either. After the charter, read the WHOLE task file at "
            "the path below the same way; it is your task.\\n\\nTask: %s\\n")
        lines += [
            f"agy_sys={shq(sys_file)}; agy_task={shq(task_file)}",
            '{ [ -r "$agy_sys" ] && [ -r "$agy_task" ]; } || '
            "{ printf 'launch.sh: the runner charter or task prompt is not readable - refusing to launch\\n' >&2; "
            "exit 1; }",
        ]
        if settings_flag:
            return [*lines, f'export {CHARTER_FILE_ENV}="$agy_sys"', *task_only]
        lines += [
            'agy_sys_n=$(wc -c < "$agy_sys"); agy_task_n=$(wc -c < "$agy_task")',
            f'if [ "$(( agy_sys_n + agy_task_n ))" -le {_INLINE_PROMPT_MAX_BYTES} ]; then',
            '  agy_prompt="$(cat "$agy_sys"; printf \'\\n\\n---\\n\\n\'; cat "$agy_task")"; set --',
            f'elif [ "$agy_task_n" -le {_INLINE_PROMPT_MAX_BYTES - 1024} ]; then',
            f'  agy_prompt="$(printf \'{charter_pointer}\' "$agy_sys"; cat "$agy_task")"; '
            'set -- --add-dir "$(dirname -- "$agy_sys")"',
            "else",
            f'  agy_prompt="$(printf \'{charter_pointer}{task_pointer}\' "$agy_sys" "$agy_task")"; '
            'set -- --add-dir "$(dirname -- "$agy_sys")"',
            "fi",
            'if [ "$(dirname -- "$agy_task")" != "$(dirname -- "$agy_sys")" ] && [ "$#" -gt 0 ]; then '
            'set -- "$@" --add-dir "$(dirname -- "$agy_task")"; fi',
            'set -- --add-dir "$PWD" "$@"',
            f'exec {argv} {quoted} "$@" -i "$agy_prompt"',
        ]
        return lines

    def api_key_prepare(self, *, config_dir: str) -> None:
        """`"modelProvider": "gemini"` merged into `$HOME/.gemini/antigravity-cli/settings.json`.

        The CLI's own changelog names the pair (that key plus `GEMINI_API_KEY`); measured on agy 1.2.11: the
        sign-in picker is skipped and the banner reads "Gemini API key". The first-run wizard a fresh home
        shows next is `prepare`'s (`_ONBOARDING_JSON`); the per-folder trust prompt is not drawn for a
        `-i` launch that adds its worktree (measured). Also creates the `TMPDIR` the account pin names
        (`antigravity_keyring.account_env`): no login ever ran in this home to make it. Non-raising."""
        if not config_dir:
            return None
        from . import agent_layer

        try:
            os.makedirs(antigravity_keyring.account_env(config_dir=config_dir)["TMPDIR"], mode=0o700,
                        exist_ok=True)
        except OSError as exc:
            log.warning("antigravity adapter: could not create the temp dir in %s (%s)", config_dir, exc)

        agent_layer.select_api_key_auth(
            config_dir=config_dir, settings_rel_path=_API_KEY_SETTINGS,
            patch={"modelProvider": _API_KEY_PROVIDER}, picker="sign-in picker",
            log=lambda message: log.warning("antigravity adapter: %s", message))
        return None

    def env(self, *, config_dir: str, worktree: str = "") -> dict[str, str]:
        """The account pin, `antigravity_keyring.account_env` — the one home of how an agy process sees its
        account, shared with the login (`adapter_env`). Nothing run-scoped is needed here (auto-approve is argv)."""
        return adapter_env(config_dir, lambda: antigravity_keyring.account_env(config_dir=config_dir))

    def prepare(self, *, worktree: str, config_dir: str, project: str) -> None:
        """The shared skills root (`agent_layer.place_shared_skills`) at `<config_dir>/.agents`, since
        `HOME` is the account dir; the first-run wizard marked done (`_ONBOARDING_JSON`), re-asserted
        every launch because it lives under the CLI's `cache/`; and the fleet's subagent catalogue as agy
        custom agents in `$HOME/.gemini/config/agents/` (`agent_layer.place_subagents`, dialect `antigravity`),
        the global agents dir both the CLI and the ACP server (`$GEMINI_HOME/config/`) read. The git-native
        hooks are installed by the harness's own session-start row, which `launch.sh` runs as the runner
        (`exec_line` step 2). Never raises."""
        del worktree, project
        if not config_dir:
            return None
        try:
            from . import agent_layer

            agent_layer.place_shared_skills(config_dir=config_dir)
            result = agent_layer.merge_json_settings(
                path=os.path.join(config_dir, _ONBOARDING_JSON), patch=_ONBOARDING_DONE)
            if not result["merged"]:
                log.warning("antigravity adapter: could not mark the first-run wizard done in %s (%s); this "
                            "runner's pane will be the wizard", config_dir, result["error"])
            agent_layer.place_subagents(dialect="antigravity", dest=os.path.join(config_dir, AGENTS_DIR))
        except Exception:  # noqa: BLE001 — a worse runner, not a no-runner
            pass
        return None

    def acp_launch(self, ctx: AcpLaunchContext) -> AcpLaunch | None:
        """`agy-acp-server --uid=` for a run whose account home is on this host; None for a run in a VM.

        The server reads hooks from the account's `$GEMINI_HOME/config/hooks.json`, the dispatcher every legacy
        runner on the account also reads, so the engine command is `bash -c`: this run's hook file — the harness
        guards and the Architectural Guard's two chains when the run is guarded (`fleet_launch.guard_chains`: the
        host does every other chain's job) — written into the account's temp dir, its path exported as
        `$JAAH_HOOKS_FILE` for the bridge, then `exec` the server. The lifecycle rows the server does not run
        (`AcpDialect.host_lifecycle`, the guard brief among them) go to the host instead, as `host_hooks`
        (`acp_hook_files`). Without the export the bridge
        would refuse every tool call (it fails closed on an unreadable hook file). `prepare` writes the account
        dispatcher (`_write_account_hooks`) and the legacy `prepare`'s files.

        An API-key account signs in through ACP `authenticate` (`ACP_KEY_SIGN_IN`): the host reads the key from
        the daemon, never the environment. A signed-in account passes no `authenticate`: `prepare` writes the
        server's own token from the CLI's sign-in (`antigravity_acp_auth.seed_oauth_sign_in`) and the server signs
        in from it. None for a run whose config home is not on this host, except an API-key run in a MicroVM guest
        (`ctx.guest`): its host signs in the same way, and the script itself writes the account dispatcher into the
        guest's config home, since nothing on this host writes there. A signed-in run in a guest stays legacy
        (`microvm-signed-in-legacy-runtime`): its server token is seeded on the host as the account's uid
        (`antigravity_acp_auth.seed_oauth_sign_in`), which the guest's config share cannot take."""
        from fleet_launch import guard_chains, shq  # in-function: see the module header

        from .agent_layer import jaah_mcp_command

        guest = ctx.guest and bool(ctx.api_key_account)
        if not (ctx.config_dir and (guest or os.path.isdir(ctx.config_dir))):
            log.info("antigravity acp: task %s's account has no config home on this host (%r) — launching "
                     "the legacy runtime", ctx.task_id, ctx.config_dir)
            return None
        env = {**self.env(config_dir=ctx.config_dir, worktree=ctx.worktree),
               _GEMINI_HOME_ENV: os.path.join(ctx.config_dir, ".gemini")}
        delimiter = f"JAAH_AGY_HOOKS_{secrets.token_hex(8)}"
        refuse = "{ printf 'agy-acp-server: %s - refusing to launch\\n' "
        exports = " ".join(f'{name}="$agy_hooks"' for name in _hooks_file_env(""))
        engine_hooks, host_hooks = acp_hook_files(guard_chains(ctx.worktree, guard=ctx.guard))
        script = "\n".join([
            f'mkdir -p -- "$TMPDIR" || {refuse}\'the temp dir could not be made\' >&2; exit 1; }}',
            # The hook file and the bridge's files beside it (its session-start context, its guidance queues).
            f"find \"$TMPDIR\" -maxdepth 1 -type f -name 'jaah-agy-hooks-*' -mtime +{_HOOK_FILE_TTL_DAYS} "
            "-delete 2>/dev/null || true",
            f'agy_hooks="$TMPDIR"/jaah-agy-hooks-{shq(ctx.session_id)}.json',
            # `rm` then an exclusive create: a planted symlink must not turn this into a write elsewhere. The umask
            # stays in the subshell: the server and its tools keep the node's group-shared umask.
            f'rm -f -- "$agy_hooks" && ( umask 077; set -C; cat > "$agy_hooks" ) <<\'{delimiter}\' || '
            f"{refuse}'the hook file could not be written' >&2; exit 1; }}",
            json.dumps(engine_hooks, indent=2),
            delimiter,
            *(_guest_account_hooks_lines(os.path.join(ctx.config_dir, HOOKS_JSON), delimiter, refuse) if guest else ()),
            f"export {exports}",
            *((f"{_bridge_command('--session-start')} </dev/null || true",) if engine_hooks["session_start"] else ()),
            f"exec {' '.join(shq(a) for a in ACP_ARGV)}",
        ])
        command, *args = jaah_mcp_command()
        control = control_mcp_server(command, args, {})
        worktree, config_dir, project, api_key_account = ctx.worktree, ctx.config_dir, ctx.project, ctx.api_key_account

        def prepare() -> None:
            if os.path.isdir(worktree) and os.path.isdir(config_dir):
                self.prepare(worktree=worktree, config_dir=config_dir, project=project)
                err = _write_account_hooks(os.path.join(config_dir, HOOKS_JSON))
                if err:
                    raise RuntimeError(f"antigravity acp: could not write the account hooks.json: {err}")
                if not api_key_account and (err := antigravity_acp_auth.seed_oauth_sign_in(config_dir)):
                    raise RuntimeError(f"antigravity acp: the signed-in run cannot sign in: {err}")

        return AcpLaunch(
            argv=("bash", "-c", script, "agy-acp"),
            env=env,
            session_meta={"antigravity": {_EAGER_MCP_META: [control["name"]]}},
            mcp_servers=(control,),
            prepare=None if guest else prepare,
            authenticate={**ACP_KEY_SIGN_IN, "account": ctx.api_key_account} if ctx.api_key_account else None,
            host_hooks=host_hooks,
        )

    # ── Hooks ──────────────────────────────────────────────────────────────────────────────────────

    def render_hooks(
        self, *, settings_path: str, chains: LifecycleChains, config_dir: str = "",
    ) -> tuple[str | None, str]:
        """`(this dispatch's hook file, the assignment exporting its path)`, or `(None, "")` when nothing
        is wired. Pure: the account `hooks.json` is `write_hooks`' second write."""
        from fleet_launch import shq  # in-function: see the module header

        del config_dir
        if not chains.any_set():
            return (None, "")
        # Every spelling: the bridge may be an older or newer checkout's.
        return (json.dumps(dispatch_hooks(chains), indent=2),
                " ".join(f"{k}={shq(v)}" for k, v in _hooks_file_env(settings_path).items()))

    def write_hooks(self, *, settings_path: str, chains: LifecycleChains,
                    mode: int | None = None, config_dir: str = "") -> str:
        """Write this dispatch's hook file (`base.write_rendered_hooks`), then the account's `hooks.json`
        that dispatches to it — both as the session user. A refused write RAISES: a runner with no hooks
        would be indistinguishable from a governed one."""
        flag = write_rendered_hooks(
            self, settings_path=settings_path, chains=chains, mode=mode, config_dir=config_dir)
        if not flag or not config_dir:
            return flag
        path = os.path.join(config_dir, HOOKS_JSON)
        err = _write_account_hooks(path)
        if err:
            raise RuntimeError(f"write_settings: could not write {path}: {err}")
        return flag

    # ── Session identity + ingest: ONE-LINE delegations (Architecture §5.1), late-bound so the module stays the
    # answer; `session_binding` is `AdapterDefaults`' (`antigravity_transcript.SESSION_BINDING` pins it equal) ──

    def discover_session_id(self, *, config_dir: str, worktree: str, since: float) -> str | None:
        return antigravity_transcript.discover_session_id(config_dir=config_dir, worktree=worktree, since=since)

    def transcript(self, *, config_dir: str, worktree: str, session_id: str) -> str | None:
        return antigravity_transcript.transcript(config_dir=config_dir, worktree=worktree, session_id=session_id)

    def transcript_root(self, *, config_dir: str) -> str:
        return antigravity_transcript.transcript_root(config_dir=config_dir)

    def session_members(self, *, config_dir: str, transcript: str) -> list[tuple[str, str]]:
        return antigravity_transcript.session_members(config_dir=config_dir, transcript=transcript)

    def read_usage(self, line: bytes) -> TokenCounts | None:
        return antigravity_transcript.read_usage(line)

    def turn_error(self, *, transcript: str) -> TurnError | None:
        return antigravity_transcript.turn_error(transcript=transcript)


__all__ = ["ACP_ARGV", "ACP_KEY_SIGN_IN", "AntigravityAdapter", "HOOKS_JSON", "HOOKS_FILE_ENV", "account_hooks_json",
           "acp_hook_files", "dispatch_hooks", "translate_harness"]
