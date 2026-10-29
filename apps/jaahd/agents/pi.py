"""agents/pi.py — the Pi `AgentAdapter`: a fleet runner on Pi (earendil-works/pi), driven over ACP by `pi-acp`.

Read off pi 1.0.2 and pi-acp 0.0.34 (the pins in `packaging/platform-pins.toml`). `pi-acp` speaks ACP on stdio
and spawns `pi --mode rpc --no-themes` for each session, as the command `PI_ACP_PI_COMMAND` names. Pi has no legacy
runtime here (`agent_providers.PI`): the launch is `acp_launch`, under `jaah-acp-host`.

Two gaps in pi-acp shape the launch, both closed node-side rather than in a fork (architecture A2):
  1. pi-acp keeps the ACP `mcpServers` of `session/new` and never hands them to pi. Pi reads its own `mcp.json`,
     so the control server is written there, with `direct` exposure so its tools are declared to the model.
  2. pi-acp starts pi with no model argument. The run's model rides `--model`, added by the command pi-acp
     spawns: `PI_ACP_PI_COMMAND` names `pi_rpc.py`, which writes Pi's files and execs `pi`.

`pi_rpc.py` runs AS THE ENGINE (a child of pi-acp, under the host), on a static node and inside a MicroVM alike, so
the files it writes are the runner uid's and their paths are the ones the engine sees. Its whole input is the
`JAAH_PI_LAUNCH` document this adapter composes; nothing in it is a secret.

The key (A9, the org's Anthropic API key) is never in the environment and never on disk: Pi's `auth.json` holds the
credential-broker client as a `!command`, which Pi runs when it first needs the key. The client is a descendant of
the host, so the broker resolves the run's lease by process ancestry and serves only its own account.
"""
from __future__ import annotations

import functools
import json
import os
import sys

import agent_providers

from .base import (
    ACP_CHAIN_EVENTS,
    ACP_HARNESS_EVENTS,
    CLAUDE_ONLY_MATCHERS,
    CONTROL_TOOL_MATCHERS,
    CONTROL_TOOL_NAMES,
    TASK_ARGV_POSITIONAL,
    AcpLaunch,
    AcpLaunchContext,
    AdapterDefaults,
    LifecycleChains,
    QuitSignature,
    TokenCounts,
    acp_hook_files as base_acp_hook_files,
    adapter_env,
    control_mcp_server,
    harness_dispatch,
    tool_path,
)

#: The ACP agent: pi-acp, which owns the ACP side and spawns pi itself. The catalogue's `binary`, so a node attests it.
ACP_BINARY = agent_providers.PI.binary
#: The command pi-acp spawns in place of `pi` (it reads `PI_ACP_PI_COMMAND`): this tree's `pi_rpc.py`, executable,
#: at the same path on a node and in a MicroVM guest (the packaged tree is a toolchain share).
RPC_COMMAND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_rpc.py")
#: The variable `pi_rpc.py` reads its launch document from.
LAUNCH_ENV = "JAAH_PI_LAUNCH"
#: Pi's config-home variable; unset, Pi uses `~/.pi/agent`.
AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
#: The Pi model provider an Anthropic API key serves.
MODEL_PROVIDER = "anthropic"
#: Pi's own automatic network activity switched off (Pi's environment-variables doc): `PI_OFFLINE` gates the
#: model-catalogue refresh, the package manager and the unpinned `fd`/`rg` download its `find`/`grep` tools would
#: fetch; the other two the `pi.dev` version request and install telemetry with provider attribution headers. Left:
#: pi-acp's own `npm view` of pi's latest version on each `session/new` (800 ms cap, the version number only).
QUIET_ENV: tuple[tuple[str, str], ...] = (("PI_OFFLINE", "1"), ("PI_SKIP_VERSION_CHECK", "1"), ("PI_TELEMETRY", "0"))
#: What Pi is told in its settings: no startup banner in the event stream.
SETTINGS: dict = {"quietStartup": True}

#: Inert: the legacy path never starts Pi (`exec_line`).
_QUIT = QuitSignature(reason="pi-has-no-session-end-event")

# ── The hook translation table: declarations only, run by `pi_extension/jaah-harness.ts` through the dispatcher ──
# The shared rows are `base`'s (`ACP_CHAIN_EVENTS`, `ACP_HARNESS_EVENTS`, the matcher sets); only Pi's own deltas
# are spelled here. The lifecycle events are the ACP host's (`AcpDialect.host_lifecycle`), so `harness_dispatch`
# leaves them out of the engine's file.

# Claude Code's harness events -> the slot the extension dispatches them under (its Pi events: `tool_call`,
# `tool_result` — `PostToolUseFailure` when the result is an error — and `session_before_compact`).
_HARNESS_EVENTS: dict[str, str | None] = {**ACP_HARNESS_EVENTS, "PostToolUseFailure": "PostToolUseFailure"}

# Claude's tool matchers -> Pi's, as FULL-match regexes over Pi's tool name (pi 1.0.2's built-in tools: `bash`,
# `read`, `edit`, `write`, `grep`, `find`, `ls`; a server's `mcp__<server>__<tool>`). None = dropped. An unlisted
# matcher RAISES (`base.harness_groups`).
_HARNESS_MATCHERS: dict[str, str | None] = {
    "Edit|Write|MultiEdit": "edit|write",
    "Bash": "bash",
    "Read|Bash": "read|bash",
    "Read|Grep|Glob": "read|grep|find",
    # The question capture: the ACP host files the question tool's rounds itself (`question_call_prefix`).
    "AskUserQuestion": None,
    **CLAUDE_ONLY_MATCHERS,
    **CONTROL_TOOL_MATCHERS,
}

# Pi's tool name -> Claude's, respelled by the dispatcher in the payload a hook reads (`hook_bridge_core.dispatch`).
# Pi's `edit` takes several replacements (`edits[]`), so it is Claude's `MultiEdit`.
_TOOL_NAMES: dict[str, str] = {
    "bash": "Bash", "read": "Read", "edit": "MultiEdit", "write": "Write", "grep": "Grep", "find": "Glob", "ls": "LS",
    **CONTROL_TOOL_NAMES,
}

# Pi's tool-input keys -> Claude's, per tool (pi 1.0.2's schemas: `read`/`edit`/`write` name the file `path`, an
# `edit` holds `edits[{oldText, newText}]`); the extension respells them in the payload, inside `edits[]` too, and
# drops a model-supplied key already spelled as a target. Unlisted keys pass as they are.
_TOOL_INPUTS: dict[str, dict[str, str]] = {
    "read": {"path": "file_path"},
    "write": {"path": "file_path"},
    "edit": {"path": "file_path", "oldText": "old_string", "newText": "new_string"},
}

# A user turn's literal text in a line of Pi's session file (pi 1.0.2's session-format doc, compact JSON; counted in
# a recorded session by `tests/acp_replay/record_pi_harness.py`): how the read-once guard counts turns
# (the payload's `transcript_turn_mark`).
TRANSCRIPT_TURN_MARK = '"message":{"role":"user"'

dispatch_hooks = functools.partial(harness_dispatch, vendor=agent_providers.PI.value, chain_events=ACP_CHAIN_EVENTS,
                                   events=_HARNESS_EVENTS, matchers=_HARNESS_MATCHERS, tool_names=_TOOL_NAMES)


def acp_hook_files(chains: LifecycleChains) -> tuple[dict, dict]:
    """A run's two hook files (`base.acp_hook_files`): the engine's (the extension's tool and compaction events)
    and the host's own."""
    return base_acp_hook_files(chains, vendor=agent_providers.PI.value, dispatch=dispatch_hooks)


class PiAdapter(AdapterDefaults):
    """Pi under the ACP host. Stateless — the registry holds one instance."""

    provider = "pi"

    # ── ACP launch ─────────────────────────────────────────────────────────────────────────────────

    def acp_launch(self, ctx: AcpLaunchContext) -> AcpLaunch:
        """`pi-acp` with `pi_rpc.py` as the pi it spawns, and the `JAAH_PI_LAUNCH` document that command reads.

        The control server is one entry, handed to the host (which passes it in `session/new`, where pi-acp drops
        it) and written by `pi_rpc.py` into Pi's `mcp.json`, where Pi reads it. No `prepare`: the files are written
        by the engine itself, as the runner uid, at the paths it sees (a MicroVM's guest paths included).

        The hook layer splits as Antigravity's does (`acp_hook_files`): the engine's file rides the document, read by
        the extension `pi_rpc.py` loads (`pi_extension/jaah-harness.ts`) and handed to the dispatcher on its stdin
        (run by this daemon's interpreter, by absolute path), so no hook file selects the guards; the lifecycle rows
        go to the host as `host_hooks`."""
        from fleet_launch import guard_chains  # noqa: PLC0415 — fleet_launch imports this package

        from . import agent_layer  # a daemon-facing sibling: local, so this module stays importable alone

        control = control_mcp_server(*_split(agent_layer.jaah_mcp_command()), {})
        engine_hooks, host_hooks = acp_hook_files(guard_chains(ctx.worktree, guard=ctx.guard))
        document = {"model": ctx.model, "provider": MODEL_PROVIDER, "key_command": key_command(ctx.api_key_account),
                    "settings": SETTINGS, "mcp_servers": [control], "hooks": engine_hooks,
                    "dispatcher": tool_path("hook_bridge_core.py"), "python": sys.executable,
                    "tool_inputs": _TOOL_INPUTS,
                    "ask_tool": agent_providers.PI.ask_tool, "no_answer": agent_providers.PI.ask_dismissed_result,
                    "turn_mark": TRANSCRIPT_TURN_MARK}
        env = {**dict(QUIET_ENV), "PI_ACP_PI_COMMAND": RPC_COMMAND, LAUNCH_ENV: json.dumps(document, sort_keys=True)}
        if ctx.config_dir:
            env[AGENT_DIR_ENV] = ctx.config_dir
        return AcpLaunch(argv=(ACP_BINARY,), env=env, session_meta={}, mcp_servers=(control,), host_hooks=host_hooks)

    # ── Legacy launch: none ────────────────────────────────────────────────────────────────────────

    base_command = (ACP_BINARY,)
    #: Inert: the legacy exec line starts nothing.
    task_delivery_kind = TASK_ARGV_POSITIONAL
    tui_vocabulary = None
    quit_sig = _QUIT
    #: The key comes from the `auth.json` helper command, never a variable (`api_key_helper` True).
    api_key_variable = ""
    mcp_binary = ""

    def exec_line(
        self, *, sys_file: str, task_file: str, settings_flag: str,
        model_args: tuple[str, ...] = (), session_args: tuple[str, ...] = (), rc_flag: str = "",
    ) -> list[str]:
        """Pi has no legacy runtime (`acp-runtime-only`, so the control plane always sends `acp`): a legacy launch
        anyway — an older control plane — says so in the pane and exits, rather than starting a CLI nobody drives."""
        del sys_file, task_file, settings_flag, model_args, session_args, rc_flag
        return ["printf 'launch.sh: Pi runs only on the ACP runtime, and this launch is a legacy one\\n' >&2",
                "exit 78"]

    def env(self, *, config_dir: str, worktree: str = "") -> dict[str, str]:
        """The account pin, Pi's config home (`adapter_env`). Only the ACP launch reads it."""
        del worktree
        return adapter_env(config_dir, {AGENT_DIR_ENV: config_dir})

    def prepare(self, *, worktree: str, config_dir: str, project: str) -> None:
        """Nothing: the ACP launch's engine writes its own files (`pi_rpc.py`)."""
        return None

    def register_mcp(self, *, config_dir: str) -> None:
        """Nothing: `pi_rpc.py` declares the control server in Pi's `mcp.json` at each start."""
        return None

    def render_hooks(
        self, *, settings_path: str, chains: LifecycleChains, config_dir: str = "",
    ) -> tuple[str | None, str]:
        """No hook file: the ACP launch carries its hooks in its document (`acp_launch`)."""
        del settings_path, chains, config_dir
        return (None, "")

    def credential_state(self, *, config_dir: str) -> str:
        """`none`: an API-key account keeps no credential on a node (the key is served by the broker)."""
        import agent_config_dirs  # noqa: PLC0415 — the three-state vocabulary's home; a daemon module

        del config_dir
        return agent_config_dirs.CREDS_NONE

    # ── Session identity + ingest: the host's event stream, never a vendor transcript ───────────────
    # `session_binding`, `discover_session_id` and `session_members` are `AdapterDefaults`' answers.

    def transcript(self, *, config_dir: str, worktree: str, session_id: str) -> str | None:
        """None: the run's record is the host's normalised event stream, collected by the ACP ingest."""
        return None

    def transcript_root(self, *, config_dir: str) -> str:
        """Pi's own session store, narrowed to its directory (nothing reads it)."""
        return os.path.join(config_dir, "sessions")

    def read_usage(self, line: bytes) -> TokenCounts | None:
        return None


def key_command(api_key_account: str) -> str:
    """Pi's `auth.json` key for an API-key account: `!` and the broker client asked for that account's key, the same
    command a Claude runner's `apiKeyHelper` runs (`fleet_dispatch.api_key_helper_command`); "" with no account."""
    if not api_key_account:
        return ""
    import account_auth  # noqa: PLC0415 — daemon modules, imported at call time like the rest of this package
    import fleet_launch  # noqa: PLC0415

    name = f"{account_auth.NODE_CREDENTIAL_PREFIX}{api_key_account}"
    if not account_auth.slug_from_node_credential_name(name):
        return ""
    return f"!{fleet_launch.shq(account_auth.CREDENTIAL_HELPER)} {fleet_launch.shq(name)}"


def _split(command: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    return command[0], tuple(command[1:])


__all__ = ["PiAdapter", "key_command"]
