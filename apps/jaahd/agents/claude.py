"""agents/claude.py — the Claude Code adapter.

Everything this daemon knows about the `claude` CLI's grammar lives here (argv, prompt delivery,
`--settings` hook schema and event names, the account env var); callers reach it only through
`agents.get_adapter(provider)`.

Byte-identity is pinned: `launch.sh` and `--settings` JSON are asserted against
`tests/golden/claude_launch.sh` and `tests/golden/claude_settings.json`. The DOUBLE SPACE after the base
argv (empty `rc_flag`) and the hook-event ORDER are part of that contract — do not tidy.

Daemon imports are in-function: `fleet_launch` imports this package (cycle), and it keeps this a cheap leaf.
"""
from __future__ import annotations

import json
import os

import agent_providers  # the provider catalogue: the ONE home of each provider's binary name
import host_platform  # the OS seam: Windows reads the charter from its file (`exec_line`)

from .base import (
    AcpLaunch,
    AcpLaunchContext,
    ConsentDialog,
    LifecycleChains,
    QuitSignature,
    SESSION_ASSIGN,
    TASK_ARGV_POSITIONAL,
    TokenCounts,
    WINDOWS_HOOK_SHELL_UNESCAPING_BASH,
    CLAUDE_CONTROL_SERVER_NAME,
    AdapterDefaults,
    adapter_env,
    chain_hook_groups,
    control_mcp_server,
    host_hook_commands,
)

# The base invocation every visible runner execs. The binary comes from the catalogue — the name
# `runner_clis.py` attests on PATH — so this can never launch a binary the node did not attest.
_BASE: tuple[str, ...] = (agent_providers.CLAUDE.binary, "--dangerously-skip-permissions")

# The startup consents Claude Code can raise before a runner's first turn, each pre-seeded elsewhere
# (`vis_trust`, `plugin_repair`'s managed-settings consent, `skipDangerousModePermissionPrompt`,
# `apiKeyHelper`) and accepted here only when that seed missed. Measured on 2.1.296: the trust,
# MCP and API-key rows on a live pane; the bypass and managed-settings strings from the binary. Managed
# settings opens focused on "Yes" (2.1.300's `focus: "confirm"`) and can sit on a runner's pane with no option
# rows drawn, so it is accepted with `Enter` alone.
_CONSENT_DIALOGS: tuple[ConsentDialog, ...] = (
    ConsentDialog("managed settings", "Managed settings require approval", "Yes, I trust these settings",
                  enter_accepts=True),
    ConsentDialog("folder trust", "Quick safety check:", "Yes, I trust this folder"),
    ConsentDialog("bypass permissions", "running in Bypass Permissions mode", "Yes, I accept"),
    ConsentDialog("project MCP server", "New MCP server found in this project", "Use this MCP server"),
    # Several servers: every one is pre-ticked, so accepting is moving to the submit row.
    ConsentDialog("project MCP servers", "new MCP servers found in this project", "Enable selected"),
    ConsentDialog("custom API key", "Detected a custom API key in your environment", "Yes"),
)

# `LifecycleChains` field -> Claude Code event name, in the generated JSON's (pinned) order.
# `PreToolUse` carries the Architectural Guard's per-write check on a guarded session; no matcher, so it sees every
# tool and exits at once for one that writes no file. `PostToolUse` is unwired today but listed so it renders.
_EVENTS: tuple[tuple[str, str], ...] = (
    ("on_session_start", "SessionStart"),
    ("on_turn_end", "Stop"),
    ("on_session_end", "SessionEnd"),
    ("on_error", "StopFailure"),
    ("on_prompt_submit", "UserPromptSubmit"),
    ("on_pre_tool", "PreToolUse"),
    ("on_post_tool", "PostToolUse"),
)



# The ACP agent for an API-key run (architecture C14): the upstream Agent SDK adapter, pinned in
# `packaging/platform-pins.toml` (its `bin`). It drives the Claude Code CLI its SDK pins, on the account's
# config home, so the plugin layer, skills, subagents and slash commands are the CLI's own.
ACP_BINARY = agent_providers.CLAUDE.acp_binary
# Every source the CLI reads on the legacy path: the account home (OMC plugins, managed settings), the repo's
# Claude Code project settings and their local overrides.
_ACP_SETTING_SOURCES = ("user", "project", "local")


class ClaudeAdapter(AdapterDefaults):
    """The `claude` CLI. Stateless — the registry holds one instance."""

    provider = "claude"

    # ── Launch ─────────────────────────────────────────────────────────────────────────────────────

    base_command = _BASE
    #: A positional argument — see `exec_line`'s last line.
    task_delivery_kind = TASK_ARGV_POSITIONAL
    #: None: the task prompt rides the argv, so there is no pane readiness marker. Answer-delivery TUI
    #: constants live in `answer_delivery`.
    tui_vocabulary = None
    #: `""`: the key rides `api_key_settings`' `apiKeyHelper`, never the environment (see there).
    api_key_variable = ""
    #: `prompt_input_exit` alone: Claude Code's `SessionEnd` enum already separates a prompt quit from
    #: `clear`, `resume`, `logout` and `other` (verified on 2.1.251 by typing `/exit`).
    quit_sig = QuitSignature(reason="prompt_input_exit")

    def exec_line(
        self, *, sys_file: str, task_file: str, settings_flag: str,
        model_args: tuple[str, ...] = (), session_args: tuple[str, ...] = (), rc_flag: str = "",
    ) -> list[str]:
        """Claude's whole `exec` block for a dispatch's `launch.sh`.

        Every interpolated element is `shq`'d, `model_args` included: `projects.default_model` is
        free-form operator input landing in a script the node executes (white-box finding C2).
        `settings_flag` arrives quoted by `render_hooks` and is NOT re-quoted.

        Charter via `--append-system-prompt`, task prompt positional, both by command substitution so
        file bytes never pass through argv quoting twice."""
        from fleet_launch import shq  # in-function: fleet_launch imports this package (see the header)

        quoted = " ".join(shq(a) for a in (*model_args, *session_args))
        # `rc_flag` is quoted too (C2's shape otherwise). Empty stays empty, not `''`: no stray argument,
        # and it keeps the pinned double space below.
        rc = shq(rc_flag) if rc_flag else ""
        # Windows caps a whole command line at 32,767 characters and the charter alone is ~120 kB, so there
        # the CLI reads it from the file ("Argument list too long" on a real PC).
        charter = (f"  --append-system-prompt-file {shq(sys_file)} \\" if host_platform.is_windows()
                   else f"  --append-system-prompt \"$(cat {shq(sys_file)})\" \\")
        return [
            # The double space (empty `rc_flag`) is in the pinned golden file.
            f"exec {' '.join(self.base_argv(model=''))} {rc} {quoted} {settings_flag} \\",
            charter,
            f"  \"$(cat {shq(task_file)})\"",
        ]

    def consent_dialogs(self) -> tuple[ConsentDialog, ...]:
        """Claude Code's startup consents (`_CONSENT_DIALOGS`)."""
        return _CONSENT_DIALOGS

    def api_key_settings(self, helper: str) -> dict:
        """`apiKeyHelper`, because a key in the environment meets the CLI's first-run "use this API key?"
        prompt; `skipDangerousModePermissionPrompt`, because an API-key runner's home may not be converged yet
        (or may be `~/.claude`), so no account template is sure to answer the bypass-mode prompt. Either prompt parks an unattended runner forever."""
        return {"apiKeyHelper": helper, "skipDangerousModePermissionPrompt": True}

    def additional_dirs_settings(self, dirs: "tuple[str, ...]") -> dict:
        """`permissions.additionalDirectories`: the CLI's file tools and search reach each of DIRS."""
        return {"permissions": {"additionalDirectories": list(dirs)}} if dirs else {}

    def env(self, *, config_dir: str, worktree: str = "") -> dict[str, str]:
        """`CLAUDE_CONFIG_DIR` — the one variable that moves this CLI onto an account — and, on Windows, the
        run-scoped `CLAUDE_CODE_GIT_BASH_PATH`: the bash the CLI runs its Bash tool and hooks in (a Windows path,
        `host_platform.windows.git_bash`).

        Built by `adapter_env`. `worktree` is unread: Claude isolates only the config dir (so `node_pinned` is
        False). `CLAUDE_SECURESTORAGE_CONFIG_DIR` is per-dispatch (`shared_storage`)."""
        run: dict[str, str] = {}
        if host_platform.is_windows():
            from host_platform import windows  # in-function: Windows-only, as `exec_line`'s charter read

            run["CLAUDE_CODE_GIT_BASH_PATH"] = windows.git_bash()
        return adapter_env(config_dir, {"CLAUDE_CONFIG_DIR": config_dir}, run)

    def prepare(self, *, worktree: str, config_dir: str, project: str) -> None:
        """Workspace trust and first-run onboarding pre-accepted (`fleet_launch.vis_trust`), keyed on WORKTREE:
        in the session user's `~/.claude.json` and, when an account was leased, in CONFIG_DIR's own. Else an
        interactive runner parks on the trust dialog. Raises when a write is refused (`vis_trust`).
        Then the account's `agents/` gets the bundle's subagents (`agent_layer.place_subagents`, a no-op
        outside the community edition; non-raising)."""
        import fleet_launch  # in-function: fleet_launch imports this package

        from . import agent_layer

        fleet_launch.vis_trust(worktree, (config_dir,) if config_dir else ())
        if config_dir:
            agent_layer.place_subagents(dialect=agent_layer.CLAUDE_DIALECT, dest=os.path.join(config_dir, "agents"))

    def acp_launch(self, ctx: AcpLaunchContext) -> AcpLaunch:
        """The run under `jaah-acp-host` (an API-key account or the Jaah account; a subscription login never gets
        here, A5).

        Each legacy launch fact is routed through `_meta.claudeCode.options`, the SDK's own option set: the
        guard hooks and `apiKeyHelper` as `settings` (the object the legacy `--settings` file
        holds, minus the chains the host now does itself — `fleet_launch.guard_chains`); the account home as
        `CLAUDE_CONFIG_DIR` with every `settingSources`, so the plugin layer and managed settings load as they
        do in the CLI; the model as `ANTHROPIC_MODEL`, the one spelling the agent never overrides. The charter
        is the host's (`systemPrompt` append), `--add-dir` is ACP's `additionalDirectories`, and resume is ACP's
        `session/load`. Prompt suggestions stay off, as `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false` keeps them
        on the legacy path: the agent turns them on unless told."""
        import fleet_launch  # in-function: fleet_launch imports this package (see the header)
        from .agent_layer import jaah_mcp_command

        content, _ = self.render_hooks(settings_path="", chains=fleet_launch.guard_chains(ctx.worktree,
                                                                                         guard=ctx.guard))
        settings = json.loads(content) if content else {}
        if ctx.key_helper:
            settings.update(self.api_key_settings(ctx.key_helper))
        if ctx.bedrock is not None:
            # The legacy `--settings` file's Jaah-account keys (`with_bedrock_settings`): `awsCredentialExport` is
            # the broker COMMAND, and `env` the plan's non-secret pairs; no AWS secret is ever written here.
            import bedrock_launch  # in-function: the daemon module, outside this stdlib-only package

            settings.update(bedrock_launch.settings(ctx.bedrock, fleet_launch.shq))
            settings["env"] = {**settings.get("env", {}), **dict(bedrock_launch.env_pairs(ctx.bedrock))}
        options: dict = {"settingSources": list(_ACP_SETTING_SOURCES), "promptSuggestions": False,
                         "settings": settings}
        env = self.env(config_dir=ctx.config_dir, worktree=ctx.worktree)
        if ctx.model:
            env["ANTHROPIC_MODEL"] = ctx.model
        command = jaah_mcp_command()
        # Workspace trust as on the legacy path (`prepare`): the account home the agent loads is the CLI's own.
        def seed_trust() -> None:
            self.prepare(worktree=ctx.worktree, config_dir=ctx.config_dir, project=ctx.project)

        # Under the plugin's spelling, which replaces the plugin's own server (`CLAUDE_CONTROL_SERVER_NAME`).
        server = control_mcp_server(command[0], command[1:], {}, name=CLAUDE_CONTROL_SERVER_NAME)
        return AcpLaunch(argv=(ACP_BINARY,), env=env, session_meta={"claudeCode": {"options": options}},
                         mcp_servers=(server,), prepare=seed_trust)

    # ── Hooks ──────────────────────────────────────────────────────────────────────────────────────

    def render_hooks(
        self, *, settings_path: str, chains: LifecycleChains, config_dir: str = "",
    ) -> tuple[str | None, str]:
        """`(the --settings JSON, the flag that installs it)`, or `(None, "")` when nothing is wired. No
        `permissions.deny`: which memory tools a session has is the task's memory arm's to decide, in
        `launch.sh` (`project_memory_bundle.ENV_OMC_DISABLE_TOOLS`), never a per-runner deny list.

        Schema: `{"hooks": {<Event>: [{"hooks": [{"type": "command", "command": …}]}]}}`, `indent=2`.
        `config_dir` is unread: `--settings` takes the file wherever `settings_path` points.

        No per-hook `"shell"` field: on Windows the CLI already runs a command hook through the Git Bash
        `CLAUDE_CODE_GIT_BASH_PATH` names (`launch.sh` exports it), as the chains need.

        On Windows the CLI collapses each `\\\\` pair before bash parses the command, so every backslash is
        doubled (`base.host_hook_commands`, measured with Claude Code 2.1.289 on a real PC)."""
        from fleet_launch import shq  # in-function: see the module header

        rendered: dict = {}
        if chains.any_set():
            rendered["hooks"] = host_hook_commands(chain_hook_groups(chains, _EVENTS), WINDOWS_HOOK_SHELL_UNESCAPING_BASH)
        if not rendered:
            return (None, "")
        return (json.dumps(rendered, indent=2), f"--settings {shq(settings_path)}")

    def register_mcp(self, *, config_dir: str) -> None:
        """No-op: the control-plane MCP server ships in the `jaah` plugin, which `plugin_repair.py`
        converges onto every account's config dir."""
        return None

    # ── Session identity + ingest ──────────────────────────────────────────────────────────────────

    def session_binding(self) -> str:
        """ASSIGN: the dispatcher mints the uuid and passes `--session-id`, binding before the process exists."""
        return SESSION_ASSIGN

    # `turn_error` stays None: Claude Code's own `StopFailure` hook reports an API-error turn.

    def discover_session_id(self, *, config_dir: str, worktree: str, since: float) -> str | None:
        """Never called for an ASSIGN vendor; None rather than a guess off the transcript filename."""
        return None

    def credential_state(self, *, config_dir: str) -> str:
        """Delegates to `agent_config_dirs.creds_state` (three-state read, shared-store redirect), which
        reads through `base.credential_file_present` like its siblings. Never reads or returns the token
        value."""
        import agent_config_dirs

        return agent_config_dirs.creds_state(config_dir)

    def transcript(self, *, config_dir: str, worktree: str, session_id: str) -> str | None:
        """`<config_dir>/projects/<munge(worktree)>/<session_id>.jsonl` when it exists, else None.

        Composed by `session_sweep.main_transcript`, the single home of the munge."""
        import session_sweep

        path = session_sweep.main_transcript(config_dir, worktree, session_id)
        return str(path) if os.path.isfile(path) else None

    def transcript_root(self, *, config_dir: str) -> str:
        """`<config_dir>/projects` — where every transcript of this vendor lives, on any tree.

        Uncalled for this adapter (conformance only): ASSIGN ids never reach the vendor containment path;
        ingest uses the narrower `break_cause.project_dir`."""
        return os.path.join(config_dir, "projects")

    def session_members(self, *, config_dir: str, transcript: str) -> list[tuple[str, str]]:
        """`[]`: uncalled for this adapter — the sweep reads Claude's `<session>/subagents/` itself
        (`session_sweep.subagent_members`)."""
        return []

    def read_usage(self, line: bytes) -> TokenCounts | None:
        """One transcript line's spend, or None when the line reports none.

        Delegates to `session_sweep.Analytics`, the single home of the billing fold. Fed one line, so
        `usage_by_model()` holds at most one entry."""
        import session_sweep

        fold = session_sweep.Analytics()
        fold.feed(line)
        by_model = fold.usage_by_model()
        if not by_model:
            return None
        model, classes = next(iter(by_model.items()))
        return TokenCounts(model=model, **classes)


# ── fleet_dispatch / reconcile: the default runtime's launch and probe facts ──
# Imported by those orchestration modules under vendor-neutral names, so the default provider's legacy
# runtime is named here and not there (a vendor-name census test lists it as known debt).

#: The variable that points this CLI at one account's config home (the one `ClaudeAdapter.env` sets).
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
#: The CLI's own config home under `$HOME` when `CONFIG_DIR_ENV` is unset. A defined-but-EMPTY
#: `CLAUDE_SECURESTORAGE_CONFIG_DIR` resolves there too in the shipped CLI — a different (possibly someone
#: else's) credential store — so the securestorage export is omitted, never empty.
HOME_DIR_NAME = ".claude"
#: That home as an operator reads it in a log line.
HOME_DIR_SHOWN = f"~/{HOME_DIR_NAME}"
#: The `launch.sh` export that turns prompt suggestions off. Claude Code's prompt-suggestion turn guesses the
#: operator's next prompt; in a runner it surfaces as an AskUserQuestion nobody should answer. Off
#: for every runner, never for a console tab (`clauded_wt`). Other CLIs ignore the name;
#: `capture_askuserquestion` still denies a leak.
PROMPT_SUGGESTION_OFF_EXPORT = "export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false"


def home_config_dir(home: str) -> str:
    """The CLI's own config home for a process whose `$HOME` is HOME (`HOME_DIR_NAME` under it)."""
    return f"{home}/{HOME_DIR_NAME}"


def session_user_config_dir() -> str:
    """The session user's own config home — where a runner launched with no `CONFIG_DIR_ENV` (an API-key
    account with no home of its own) registers its session; `""` with no session user
    (`session_user.default_claude_config_dir`)."""
    import session_user  # in-function: a daemon module, outside this cheap leaf (see the header)

    return session_user.default_claude_config_dir()


# ── collector / session_sweep: the default runtime's on-disk facts ──
# The account probe (`collector`) and the transcript sweep (`session_sweep`) import these under
# vendor-neutral names, so the default provider's legacy runtime is named here and not there
# (a vendor-name census test lists it as known debt). Plain data: no import cost beyond this leaf.

#: The OAuth-scoped API the account probe reads `/api/oauth/profile` and `/api/oauth/usage` from.
OAUTH_API = "https://api.anthropic.com"
#: The beta header every OAuth-scoped API request carries, and its value.
OAUTH_BETA_HEADER = "anthropic-beta"
OAUTH_BETA = "oauth-2025-04-20"
#: Refresh-token grant: best-evidenced, NOT officially documented; if it changes a refresh fails and the
#: account falls back to re-login. `platform.claude.com` sits behind Cloudflare, which rejects urllib's
#: default agent with `403 error code: 1010` (verified: no UA → 403/1010; an own-name UA → a clean 400 on a
#: bogus token), so every OAuth request names its caller — never impersonating the `claude` CLI.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
#: The public Claude Code OAuth client id, not a secret.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow (public OAuth client id, not a secret)

#: The token file's top-level key holding the OAuth pair (`accessToken`, `refreshToken`, `expiresAt`, …).
CREDENTIAL_BLOCK = "claudeAiOauth"
#: The account home's config file, whose `ACCOUNT_CONFIG_BLOCK` caches the identity (can lag a re-login).
ACCOUNT_CONFIG_FILE = ".claude.json"
ACCOUNT_CONFIG_BLOCK = "oauthAccount"

#: The CLI's OWN refresh locks, measured in claude-code 2.1.285: proper-lockfile `mkdir`s of
#: `<storage dir>/CLI_REFRESH_LOCK` AND the legacy sibling `<storage dir>.lock` (an older CLI takes only that
#: one), `stale: 60000`, `update: 5000`; the storage dir is `CLAUDE_SECURESTORAGE_CONFIG_DIR` or the config
#: dir. A rotated refresh token dropped by a reader/re-reader/rewriter disagreeing is claude-code#54443.
CLI_REFRESH_LOCK = ".oauth_refresh.lock"
#: The CLI's `stale: 60000`, in seconds.
CLI_REFRESH_LOCK_STALE_SECONDS = 60.0

#: The model family in a model id, read as the CP's `canonical_model` reads it
#: (`apps/api/app/model_prices.py::_CLAUDE_FAMILY_RE`): the LAST `claude`-adjacent family wins. Lower-case input.
MODEL_FAMILY_PATTERN = r"(?<![a-z0-9])claude[.\-_/ ]?(?:\d[\d.\- ]*[.\-_ ]?)?(sonnet|haiku|fable|opus)(?![a-z])"
