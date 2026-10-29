#!/usr/bin/env python3
"""agent_providers.py — the closed set of agent CLIs the fleet can run a runner on: the ONE home.

Read by the node's runner and login adapters directly, and by the control plane through
`apps/api/app/agent_providers.py`, which loads THIS file by path and adds its CP-only helpers (`apps/api`
may not be imported from the node daemon, so the home is the side both can reach). Stdlib-only data: the
api image COPYs it alone.

`degradations` is a product promise, enforced by a test: complete (every
shipping loss appears under a named key) and no ghosts (a loss the design disproved is not published).
Losses shared by both providers are published under each, so the Add Account dialog reads one provider's
list directly.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True)
class Degradation:
    """One capability the fleet has on Claude Code and does not have on this provider.

    `key` is STABLE — the completeness test and the Add Account dialog key on it, so a rename is a wire
    change.
    """

    key: str
    # What is lost, in the fleet's own vocabulary -- short enough to be a list heading.
    capability: str
    # The operator-visible effect: what they will notice, not which hook is missing.
    consequence: str
    # True => a gap in the fleet's own harness on this CLI (guards, hooks, subagents, the charter) rather than
    # in the vendor's product: Settings > Providers groups these under one heading so what differs stands out.
    harness: bool


# ── Capability groups (architecture §3.4) ──────────────────────────────────────────────────────────────
# What orchestration reads INSTEAD of a provider name: a mechanism that differs by CLI asks one of these
# groups, never `value`/`binary` (a check pins it). `GET /api/accounts/providers`
# serialises them field for field. A provider child corrects its own row's values as its runtime lands.

#: The runtime-mode vocabulary (PRD R4). Reported only: every runner still launches full-access.
Mode = Literal["approval-required", "auto-accept-edits", "auto", "full-access", "plan"]
MODES: tuple[Mode, ...] = ("approval-required", "auto-accept-edits", "auto", "full-access", "plan")


@dataclass(frozen=True)
class Sessions:
    # True => the node names a new session's id up front (`--session-id`); False => it discovers it.
    assign_id: bool
    # How an ended session is reopened: the agent's own "resume", an ACP "load", or not at all.
    resume: Literal["resume", "load", "none"]
    list: bool
    close: bool
    # True => a session may reach directories beyond its worktree.
    additional_dirs: bool
    # True => the agent takes hook-added context at session start (the Architectural Guard's rule list, the
    # routing notes); False => what it must know from the first turn rides in its prompt instead
    # (`app/dispatch_compose/compose.py::_guard_rules_for`).
    start_context: bool
    # The arguments after `binary` that print the live sessions of one config dir as a JSON array (each entry's
    # `status` and `cwd`): the node's liveness probe (`reconcile.accounts_probe._default_agents_probe`,
    # `hooks/autoclose-check.py`). `()` = the CLI has no such listing.
    list_command: tuple[str, ...]


@dataclass(frozen=True)
class Turns:
    # True => a running turn takes more input without being cancelled.
    steer: bool
    interrupt: bool


@dataclass(frozen=True)
class Requests:
    # True => the agent asks before a gated tool call.
    approval: bool
    # How the agent asks the operator a question: an ACP elicitation, its own MCP-style tool, a shell command
    # (`ask_tool` is a verb the runner runs, its answer the next prompt), or not at all.
    user_input: Literal["elicitation", "mcp_tool", "command"] | None


@dataclass(frozen=True)
class Modes:
    # The modes reported for this provider (the whole vocabulary: a mode it cannot enforce is still named).
    supported: tuple[Mode, ...]
    # The subset the agent enforces itself; a supported mode outside it is "not enforced natively".
    native: tuple[Mode, ...]


@dataclass(frozen=True)
class Auth:
    # The row's own `auth_kinds`, filled by `AgentProvider.__post_init__` (one home: the row's field).
    kinds: tuple[str, ...]
    # How an API key reaches the agent on the ACP runtime.
    key_delivery: Literal["helper", "stdin", "config_file", "acp_authenticate", "none"]
    # Never True: a key in the agent's environment is a leak: no secret may sit there.
    key_in_env: Literal[False]


@dataclass(frozen=True)
class Usage:
    tokens: bool
    cost: bool
    rate_limit_reset: bool
    # True => a transcript line's price depends on when it ran, so the adapter tiers each line
    # (`line_pricing_tier`); False => one rate per model (`session_sweep.claude_cli_line_tier`).
    priced_by_time: bool
    # True => the agent's reported input count already includes its cached tokens and its thought tokens are
    # billed as output, so the ACP normaliser folds them as `acp.events.fold_inclusive_counts` does.
    input_includes_cached: bool = False


@dataclass(frozen=True)
class Console:
    chat_view: bool
    terminal_view: bool
    # True => the console switches one session between the two views.
    toggle: bool
    # Why a view is missing, shown beside it; "" when nothing is.
    reason: str = ""


@dataclass(frozen=True)
class AcpDialect:
    """What one engine's ACP adapter says outside the protocol, as data the generic host reads (`acp/events.py`,
    `acp/host.py` through `dialect_for`): the empty default is plain ACP, read by the protocol alone."""
    # The key on a prompt error's `data` naming its cause in the agent's own words (e.g. "errorKind").
    error_kind_meta: str = ""
    # The agent's kind -> a Jaah break cause (`acp.events.BREAK_CAUSES`); the error's class follows the cause.
    error_kinds: tuple[tuple[str, str], ...] = ()
    # Regex (case-insensitive) on an error's message, or on a tool-less turn's agent text -> a break cause.
    error_text_patterns: tuple[tuple[str, str], ...] = ()
    # True => the engine's prompt error and failed-turn text are the VENDOR's own API-error record (not agent
    # prose), so the cause the node's one vendor classifier reads off it (`hooks/break_cause.py::
    # classify_vendor_error`) names the break, as the terminal sensor's does (`vendor_turn_errors`).
    vendor_error_text: bool = False
    # The `_meta` key of a `usage_update` carrying the rate-limit report (`resetsAt`, `rateLimitType`, `status`).
    rate_limit_meta: str = ""
    # The prompt result's `_meta` key carrying per-model usage (`model_usage`).
    usage_meta: str = ""
    # The path, in `session/new`'s `_meta`, of an agent's own appended system prompt, for an agent that takes the
    # charter there rather than as ACP `systemPrompt` (`sessionCapabilities.systemPrompt`); `()` = it takes neither,
    # and the charter heads the first prompt.
    system_prompt_meta: tuple[str, ...] = ()
    # The tool-call id prefix of a `session/request_permission` that is the agent's own question tool, not a gated
    # tool call: the host files it as an operator question round (`acp/host.py`); "" = the agent asks none this way.
    question_call_prefix: str = ""
    # Stock elicitation message to omit when a property title already contains the question.
    question_preamble: str = ""
    # The `_meta` key that marks an elicitation property as the agent's own question tool: always the question in
    # `title`, its label in `description`, whatever their lengths (`acp/requests.py`); "" = none marked.
    question_meta_key: str = ""
    # The lifecycle events the ACP host runs in front of the engine (Jaah-owned code), a subset of
    # `HOST_LIFECYCLE_EVENTS`. Disjoint from the events the adapter renders into the engine's own hook layer, so no
    # event fires twice (a test pins it); `()` = the engine runs every one it has.
    host_lifecycle: tuple[str, ...] = ()
    # How hook context reaches the model: "native" = the engine's own hook channel; "turn_boundary" = the in-band
    # dispatch spools it (`hook_bridge_core.py`, `$JAAH_HOOK_CONTEXT`) and the host drains it into the head of
    # the next prompt it sends (`acp/host.py::Host._drain_hook_context`).
    context_channel: Literal["native", "turn_boundary"] = "native"
    # The env var naming the charter file the host writes (`<run dir>/system-prompt.md`) before `session/new`, for an
    # engine that reads its system instructions from a file; "" = none (`acp/host.py::Host._start_session`).
    system_prompt_file_env: str = ""


#: The lifecycle events a host may run itself (`AcpDialect.host_lifecycle`).
HOST_LIFECYCLE_EVENTS: tuple[str, ...] = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PreCompact")


#: The prompt result's `_meta` namespace the Claude and Codex ACP adapters report per-model usage in.
_QUOTA_USAGE = "quota"

#: claude-agent-acp's dialect, so every Claude Code row's (Claude, DeepSeek). Its `errorKind` (the prompt
#: error's `data`, the SDK's assistant `error`) maps as the StopFailure hook maps the same values
#: (`hooks/runner-status.py::_ERROR_TYPE_CAUSES`); money and holds are `credit_exhausted`, not a window. The text
#: pattern is Bedrock refusing the model to the account, exactly `hooks/break_cause.py::MODEL_REFUSED` (a test
#: pins them). Its rate-limit report rides a `usage_update`'s `_meta["_claude/rateLimit"]` (the SDK's
#: `rate_limit_info`: `resetsAt` epoch seconds, `rateLimitType`, `status`).
_CLAUDE_CODE_ACP = AcpDialect(
    error_kind_meta="errorKind",
    error_kinds=(("server_error", "api_error"), ("rate_limit", "usage_limit"), ("billing_error", "credit_exhausted"),
                 ("account_on_hold", "credit_exhausted"), ("authentication_failed", "auth_expired"),
                 ("oauth_org_not_allowed", "auth_expired"), ("overloaded", "api_error"),
                 ("invalid_request", "api_error"), ("model_not_found", "api_error"),
                 ("max_output_tokens", "api_error")),
    error_text_patterns=((r"is not available for this account\. You can explore other available models|"
                          r"anthropic\.claude[\w.:-]* is not available for this account|"
                          r"You don't have access to the model with the specified model ID|"
                          r"Model use case details have not been submitted", "model_refused"),),
    rate_limit_meta="_claude/rateLimit",
    usage_meta=_QUOTA_USAGE,
)


@dataclass(frozen=True)
class Capabilities:
    # Per auth kind, the runtime a session of that kind runs on: `(auth_kind, "acp"|"legacy_only"|"acp_only")` pairs.
    # "acp" has the operator's switch; "legacy_only" and "acp_only" have none (an engine with only one runtime).
    # Pairs, not a dict, so the frozen row stays hashable; the listing serialises them as an object.
    runtime: tuple[tuple[str, Literal["acp", "legacy_only", "acp_only"]], ...]
    sessions: Sessions
    turns: Turns
    requests: Requests
    runtime_modes: Modes
    auth: Auth
    # The node-managed account layer converged into this CLI's home: oh-my-claudecode, oh-my-codex, or none.
    account_layer: Literal["omc", "omx", "none"]
    # True => the fleet harness's project-memory hooks run on this CLI; False =>
    # an adapter translating the harness drops them (`agents/base.py::harness_groups`) until a native
    # projection for this CLI lands (deferred, D-12).
    project_memory_hooks: bool
    usage: Usage
    # The vendor id an org's data reaches through this provider's runners (`app/hipaa.py`'s BAA warnings).
    vendor: str
    # What an API key of this provider is verified against before the account is written
    # (`app/agent_providers.py::api_key_probe`); None = nothing is probed.
    api_key_probe_url: str | None
    # The lowercased model-id prefixes naming this provider's models (`app/agent_providers.py::provider_of_model`).
    model_id_prefixes: tuple[str, ...]
    console: Console
    # What the engine's ACP adapter says outside the protocol (`dialect_for`); plain ACP when left out.
    acp_dialect: AcpDialect = AcpDialect()
    # The auth kinds whose "acp" switch ships OFF (the pair runs legacy until the operator turns it on in Settings >
    # Providers): an ACP launch built but not yet seen working live. Only kinds whose `runtime` is "acp".
    acp_switch_off: tuple[str, ...] = ()
    # Why this row's "legacy_only" pairs have no switch, when no `no-acp` gap says so (`app/acp_runtime.py::
    # legacy_only_reason`); "" when it has none.
    legacy_only_reason: str = ""
    # How the node reads an account of this provider that no `account_layer` reader answers
    # (`provider_health._HEALTH_PROBES` and `_IDENTITY_QUOTA_READERS` are keyed on it): the CLI's own status verb and
    # JSON-RPC server ("app_server"), the credential file plus the Code Assist API ("code_assist"), or nothing on the
    # node because the credential broker holds the key ("broker_key"); "none" = no probe.
    health_probe: Literal["app_server", "code_assist", "broker_key", "none"] = "none"
    # The control plane's own second anchor on an authorize URL a node scraped (`app/agent_providers.py::
    # authorize_url_admits`), as `(prefix, exact)`: `exact` says the whole URL must EQUAL the prefix, otherwise it must
    # start with it. It narrows host and path, not provenance. None = no opinion (a host that varies). The node never
    # reads it; its scrape pattern is the adapter's own copy.
    authorize_url_anchor: tuple[str, bool] | None = None
    # The row's own `degradations`, filled by `AgentProvider.__post_init__` (one home: the row's field).
    gaps: tuple[Degradation, ...] = ()


def _modes(native: tuple[Mode, ...]) -> Modes:
    """Every mode reported, `native` the ones the agent enforces itself."""
    return Modes(supported=MODES, native=native)


#: The console's views: the TUI terminal everywhere, the chat view on an ACP session
#: (`apps/web/src/components/console/ChatView.tsx`). A legacy session's own reason is the CP's
#: (`app/routes/console_runtime.py`). No hand-off between the two fronts for these: their TUI has no
#: `resume_exec_line` (`agents/base.py`), so the terminal cannot reopen the chat's session.
_CHAT_AND_TERMINAL = Console(chat_view=True, terminal_view=True, toggle=False,
                             reason="The terminal can't take over a live chat session yet.")
#: Both views, and the toggle hands the one native session between `jaah-acp-host` and the TUI
#: (`apps/api/app/routes/console_runtime.py`; node half `reconcile.enact_launch._launch_into_slot`'s `handoff`): a CLI with an ACP
#: launch AND a TUI that resumes a session by id (`resume_exec_line`, asserted registry-wide by the adapter tests).
_HANDOFF = Console(chat_view=True, terminal_view=True, toggle=True)


@dataclass(frozen=True)
class AgentProvider:
    """One agent CLI a credential account can be registered against."""

    # The DB value -- what `accounts.provider` stores and what every wire payload carries.
    value: str
    # What the console shows.
    label: str
    # The executable name, as it is found on a node's PATH.
    binary: str
    # False = no quota to gate on, which is NOT a quota that could not be read — conflating them makes a
    # Claude account with a transiently failing probe the preferred pick.
    has_quota_model: bool
    # The quota slots' labels, short window first: `[0]` names the reading `fiveHourPercent` carries (a
    # window of at most a day), `[1]`, when present, the one `weeklyPercent` carries. A provider with one
    # window lists one label, and its weekly slot is "no such window", never "not read yet".
    quota_windows: tuple[str, ...]
    # How many accounts of this provider fit on ONE node; 0 = unlimited.
    max_accounts_per_node: int
    # True => `accounts.node_id` is REQUIRED: credentials are node-local, never on the shared store.
    node_pinned: bool
    # True => a Shared account's credential file (`credential_file`) is a SYMLINK from the per-node home to
    # `<share>/<slug>/<credential_file>`, because the CLI has no variable that moves only its token; the home
    # itself stays per-node. False => an env var moves the token (Claude's `CLAUDE_SECURESTORAGE_CONFIG_DIR`).
    # Meaningless when `node_pinned`.
    shared_token_link: bool
    # True => a login must run on a node that attests `binary` in `nodes.runner_clis`. False => every
    # node carries the CLI by construction (the fleet baseline).
    needs_attested_binary: bool
    # The credential file a login writes, relative to the account's CLI home (the token dir for Claude).
    # What the node's shared-store presence check looks for (`apps/jaahd/shared_storage.py`).
    credential_file: str
    # False => an autonomous-OFF task cannot be interviewed; the charter renders autonomous ON and says so.
    can_interview: bool
    # The CLI's own structured-question tool: what the charter's `AskUserQuestion` is respelled to, and
    # what the node's question-capture hook matches on.
    ask_tool: str
    # What that tool call itself returns when the node dismisses its picker with `Esc` to deliver the
    # answer as an ordinary message (`apps/jaahd/answer_delivery.py`). Measured per CLI, respelled into
    # the charter so the runner does not read the dismissal as a refusal.
    ask_dismissed_result: str
    # True => the charter was written for this CLI (its tool names, plugin, OMC roster, `Read` tool). False =>
    # the composer prepends a base-agent preamble translating them. The charter is always ADDED to the CLI's
    # built-in prompt (`--append-system-prompt`, `AGENTS.override.md`), never a substitute.
    charter_native: bool
    # How a login COMPLETES: "paste_code" (the operator pastes an authorization code back) or
    # "await_credentials" (the credentials land on disk). A SWITCH for every row, Claude's included: the
    # node's login machine drives the mode this declares and never infers one from the auth URL's shape.
    login_completion: str
    # How the charter must spell the control-plane MCP tool names for this CLI (per-vendor derivation).
    mcp_tool_prefix: str
    # Tier -> the vendor-maintained MOVING alias this CLI's `--model` takes for it, as `(tier, alias)`
    # pairs over `app/model_catalog.py::MODEL_IDENTIFIERS`. `()` = the vendor keeps no such alias, so the
    # dispatching node's own live catalog is read instead (`model_catalog.pick_from_catalog`). Either way
    # every CLI gets a `--model`, and a vendor's new model is picked up with no edit here.
    tier_aliases: tuple[tuple[str, str], ...]
    # How the charter tells this CLI to spawn one of its subagents (the tool and the argument naming it).
    subagent_spawn: str
    # Text placed in the prompt to list the subagent roster, for a CLI whose own list does not survive a
    # replaced system prompt ("" when the spawn tool's own description carries it).
    subagent_roster: str
    # The auth kinds an account of this provider may declare, over `account_auth.AUTH_KINDS`. The account
    # writer refuses any other (a DeepSeek OAuth account), so the roster cannot
    # hold a row no login or key lease can serve.
    auth_kinds: tuple[str, ...]
    # The credential type an API-key account of this provider points at (`app/credentials/registry.py`);
    # "" when `"api_key"` is not in `auth_kinds`.
    api_key_credential_type: str
    # How an API-key account is picked beside the OAuth ones: "spillover" (only once every OAuth account is
    # out of quota -- a key beside a subscription), "peer" (a first-class pick like any other account -- a
    # provider whose only kind is an API key), "" when this provider takes no API key.
    api_key_lane: str
    # True => this CLI takes an API key from a settings helper (`apiKeyHelper`, dialing the credential broker
    # per request), never its environment (node side: the adapter's `api_key_env()` is ""). The only key route a
    # console tab (`apps/jaahd/clauded_wt.py`) launches, so an API-key account of a provider with False is never
    # picked for one (`routes/console.py::_console_account`); a pool runner exports the key instead.
    api_key_helper: bool
    # True => each account carries its own concurrent-runner cap (`accounts.max_runners`), set by the
    # operator; the only bound on spend for a provider with no quota to gate on.
    per_account_runner_cap: bool
    # What a new account's `accounts.auto_assign` defaults to when the create request names none: whether
    # the scheduler may pick it for a task that did not ask for it.
    auto_assign_default: bool
    # How a task's explicit model id that names ANOTHER vendor's model is handled: "passthrough" (handed to
    # the CLI as written) or "fold" (translated to this provider's nearest tier alias).
    foreign_model_ids: str
    # The typed capability groups orchestration reads instead of a provider name (above).
    capabilities: Capabilities
    # The itemized losses relative to Claude Code, in the order the page lists them.
    degradations: tuple[Degradation, ...]
    # Executables `binary` spawns itself, found on the same PATH: attesting `binary` alone says nothing about
    # them, so the node senses them too (`runner_clis.sense_runner_clis`) and the console names a missing one
    # (`NodeOut.missing_companions`). Never a dispatch gate: dispatch matches on `binary` only.
    companions: tuple[str, ...]
    # The separate ACP agent this provider's ACP launch execs (`agents.<provider>.ACP_BINARY`), `""` when the ACP
    # launch is `binary` itself or there is none. Sensed like `binary` (`runner_clis.sense_runner_clis`), and the
    # control plane routes no run to the ACP lane on a node that does not attest it
    # (`apps/api/app/routes/fleet.py::_acp_adapter_missing`).
    acp_binary: str
    # True => this CLI runs on a Windows node and its accounts may sign in there; every other provider is
    # refused on Windows (`apps/api/app/node_platform.py`).
    windows_runner: bool
    # True => the macOS installer ships this CLI (with its ACP adapter) and its accounts may sign in there;
    # every other provider is refused on macOS (`apps/api/app/node_platform.py`).
    macos_runner: bool
    # True => this CLI ships for Linux arm64, so its accounts may run in a macOS node's MicroVM (an arm64 Linux
    # guest, no Rosetta); False refuses them there (`apps/api/app/node_platform.py::session_reason`).
    linux_arm64: bool

    def __post_init__(self) -> None:
        # `auth.kinds` and `gaps` restate two fields of this row; filled here so each keeps one home. A row
        # writes them empty; anything else would be silently replaced, so it is refused. The derived values
        # themselves pass, so `dataclasses.replace` on a built row works.
        caps = self.capabilities
        if caps.gaps not in ((), self.degradations) or caps.auth.kinds not in ((), self.auth_kinds):
            raise ValueError(f"{self.value}: write `auth.kinds` and `gaps` empty; they are this row's own fields")
        object.__setattr__(self, "capabilities", replace(
            caps, auth=replace(caps.auth, kinds=self.auth_kinds), gaps=self.degradations))


CLAUDE = AgentProvider(
    value="claude",
    label="Claude Code",
    binary="claude",
    companions=(),
    acp_binary="claude-agent-acp",
    windows_runner=True,
    macos_runner=True,
    linux_arm64=True,
    # Read from the OAuth usage endpoint (`collector._live_quota`).
    has_quota_model=True,
    quota_windows=("5h", "7d"),
    max_accounts_per_node=0,
    # Claude isolates only the TOKEN behind one env var, so its login can live on the shared store.
    node_pinned=False,
    shared_token_link=False,
    needs_attested_binary=False,
    credential_file=".credentials.json",
    can_interview=True,
    ask_tool="AskUserQuestion",
    ask_dismissed_result="User declined to answer questions",
    charter_native=True,
    # The switch: the operator pastes the code back, sealed to the node's per-login key. The
    # CLI's own loopback listener is never used — the console is the one path, on and off the tailnet.
    login_completion="paste_code",
    mcp_tool_prefix="mcp__plugin_jaah_control__",
    tier_aliases=(("max", "fable"), ("high", "opus"), ("standard", "sonnet"), ("low", "haiku")),
    subagent_spawn="the `Agent` tool, its `subagent_type` naming the agent",
    subagent_roster="",
    # Must stay empty: Claude Code is the baseline every other row is measured against.
    auth_kinds=("oauth", "api_key"),
    api_key_credential_type="anthropic_api_key",
    api_key_lane="spillover",
    api_key_helper=True,
    per_account_runner_cap=False,
    auto_assign_default=True,
    foreign_model_ids="passthrough",
    capabilities=Capabilities(
        # A subscription session stays on the legacy runtime (operator decision A5); a key runs on ACP.
        # Its key is checked by the Credentials page's own tester, so nothing is probed here.
        runtime=(("oauth", "legacy_only"), ("api_key", "acp")),
        sessions=Sessions(assign_id=True, resume="resume", list=True, close=True, additional_dirs=True,
                          start_context=True, list_command=("agents", "--json")),
        turns=Turns(steer=False, interrupt=True),
        requests=Requests(approval=True, user_input="mcp_tool"),
        runtime_modes=_modes(("approval-required", "auto-accept-edits", "full-access", "plan")),
        auth=Auth(kinds=(), key_delivery="helper", key_in_env=False),
        account_layer="omc",
        project_memory_hooks=True,
        usage=Usage(tokens=True, cost=True, rate_limit_reset=True, priced_by_time=False),
        vendor="anthropic",
        api_key_probe_url=None,
        model_id_prefixes=("claude", "fable", "opus", "sonnet", "haiku"),
        console=_CHAT_AND_TERMINAL,
        acp_dialect=_CLAUDE_CODE_ACP,
    ),
    degradations=(),
)


CODEX = AgentProvider(
    value="codex",
    label="OpenAI Codex",
    binary="codex",
    companions=(),
    acp_binary="codex-acp",
    windows_runner=True,
    macos_runner=True,
    linux_arm64=True,
    # Read through the CLI's own `codex app-server` (`provider_health._codex_identity_and_quota`).
    has_quota_model=True,
    quota_windows=("5h", "7d"),
    # Unlimited: the vendor's config-home override lets several accounts coexist on one node.
    max_accounts_per_node=0,
    # The override relocates the whole home, so a Shared account links only its credential file onto the
    # share.
    node_pinned=False,
    shared_token_link=True,
    needs_attested_binary=True,
    credential_file="auth.json",
    can_interview=True,
    # Needs `--enable default_mode_request_user_input` (the adapter's base argv): without it the tool
    # answers "unavailable in Default mode". `Esc` interrupts the turn (measured, codex-cli 0.152.1).
    ask_tool="request_user_input",
    ask_dismissed_result="aborted by user",
    charter_native=False,
    # Device auth: credentials appear on disk when the operator finishes in their browser; there is no
    # code to paste back, so paste mode would hang the login to its timeout.
    login_completion="await_credentials",
    # How this CLI spells a natively registered stdio server's tools — not checkable in this repo.
    mcp_tool_prefix="mcp__control__",
    # No moving aliases: its `codex debug models` catalog is read per node (`nodes.model_catalogs`).
    tier_aliases=(),
    # 0.151.0 lists every `$CODEX_HOME/agents/*.toml` role inside `spawn_agent`'s own description, so
    # the roster needs no prompt text (measured, `agents/subagents.py`).
    subagent_spawn="the `spawn_agent` tool, its `agent_type` naming the agent",
    subagent_roster="",
    # An OpenAI API key is the second kind: on ACP logged into the account's own home on stdin, on the legacy
    # runtime exported as `OPENAI_API_KEY` (`agents/codex.py`). Spillover, as Claude's key: the subscription
    # quota is read, so the key is spent only once it runs out.
    auth_kinds=("oauth", "api_key"),
    api_key_credential_type="openai_api_key",
    api_key_lane="spillover",
    api_key_helper=False,
    per_account_runner_cap=False,
    auto_assign_default=True,
    foreign_model_ids="passthrough",
    # Measured on codex-acp 2.2.2 / codex-cli 0.163.0: the
    # session id is the app-server thread id `session/new` returns, `session/resume` and `session/close` are
    # advertised, `additionalDirectories` is honoured, and the key is `codex login --with-api-key` on stdin.
    capabilities=Capabilities(
        runtime=(("oauth", "acp"), ("api_key", "acp")),
        sessions=Sessions(assign_id=False, resume="resume", list=True, close=True, additional_dirs=True,
                          start_context=True, list_command=()),
        turns=Turns(steer=True, interrupt=True),
        requests=Requests(approval=True, user_input="elicitation"),
        runtime_modes=_modes(("approval-required", "auto", "full-access")),
        auth=Auth(kinds=(), key_delivery="stdin", key_in_env=False),
        account_layer="omx",
        # On the ACP runtime, through its run hook file (`agents/codex.py::_dispatch_hooks`).
        project_memory_hooks=True,
        usage=Usage(tokens=True, cost=False, rate_limit_reset=True, priced_by_time=False),
        vendor="openai",
        api_key_probe_url="https://api.openai.com/v1/models",
        model_id_prefixes=("gpt-", "codex", "o3", "o4"),
        console=_HANDOFF,
        # A failed turn is `threadStatus` `systemError` then the CLI's own error text (`acp/events.py::_THREAD_FAILED`).
        # Its retries cover only 429s and 5xx, so a turn that ran out of them is an API error (the status it names is
        # read by the vendor classifier); `stream disconnected …` also prefixes a refused prompt, so it names none.
        acp_dialect=AcpDialect(
            error_text_patterns=((r"exceeded retry limit", "api_error"),),
            vendor_error_text=True, usage_meta=_QUOTA_USAGE, question_preamble="Codex needs your input to continue.",
            question_meta_key="codex"),
        health_probe="app_server",
        # Exact (`codex_login.py::_AUTH_URL_RE`): a prefix would admit a `?user_code=<attacker-code>` phishing link.
        authorize_url_anchor=("https://auth.openai.com/codex/device", True),
    ),
    degradations=(
        Degradation(
            key="device-code-login-workspace-gated",
            harness=False,
            capability="Device-code login",
            consequence=(
                "On a Team or Enterprise workspace an admin must enable device-code login before an "
                "account can be registered here; otherwise register on a node whose browser can reach "
                "the callback, and the account is pinned to that node."
            ),
        ),
        Degradation(
            key="subscription-concurrency-throttling",
            harness=False,
            capability="Concurrency under a subscription",
            consequence="Many concurrent Codex runners on one subscription may be throttled.",
        ),
    ),
)


DEEPSEEK = AgentProvider(
    value="deepseek",
    label="DeepSeek",
    # Claude Code itself, pointed at DeepSeek's Anthropic-compatible endpoint by the node adapter, so the CLI's
    # own capabilities (hooks, `AskUserQuestion`) are Claude's. The fields below describe that adapter; an
    # API-key launch currently loses the plugin layer they rely on, published as rows 37-39 below.
    binary="claude",
    companions=(),
    acp_binary="",
    windows_runner=False,
    macos_runner=False,
    linux_arm64=True,
    # DeepSeek publishes a prepaid balance, not a usage window: nothing to gate on, which is NOT a quota
    # that failed to read. The roster says "no quota".
    has_quota_model=False,
    quota_windows=(),
    max_accounts_per_node=0,
    # An API-key account materializes nothing on a node, so it is never pinned or linked onto the share.
    node_pinned=False,
    shared_token_link=False,
    # The CLI is Claude Code, which every node carries by construction.
    needs_attested_binary=False,
    credential_file="",
    can_interview=True,
    ask_tool="AskUserQuestion",
    ask_dismissed_result="User declined to answer questions",
    charter_native=True,
    # No login at all: the account is its API key, so there is no completion mode to declare.
    login_completion="",
    mcp_tool_prefix="mcp__plugin_jaah_control__",
    # The two model ids DeepSeek serves, each with the 1M-context suffix the CLI passes through.
    tier_aliases=(
        ("max", "deepseek-v4-pro[1m]"),
        ("high", "deepseek-v4-pro[1m]"),
        ("standard", "deepseek-flash[1m]"),
        ("low", "deepseek-flash[1m]"),
    ),
    subagent_spawn="the `Agent` tool, its `subagent_type` naming the agent",
    subagent_roster="",
    auth_kinds=("api_key",),
    api_key_credential_type="deepseek_api_key",
    # Its only kind is a key, so its accounts are ordinary picks rather than a spillover behind OAuth.
    api_key_lane="peer",
    api_key_helper=True,
    # With no quota to gate on, the operator's per-account cap is what bounds spend.
    per_account_runner_cap=True,
    # Off: a new DeepSeek account runs only the tasks that ask for it until the operator opts it in.
    auto_assign_default=False,
    # A task pinned to a Claude model id is translated to the nearest DeepSeek tier, never sent as written.
    foreign_model_ids="fold",
    # Rows 16-23 were seeded from DeepSeek's published Claude Code compatibility notes and each was
    # re-measured on dev (against the endpoint and on live runners); rows 37-39 are that
    # run's failures: an API-key account launches with no config dir, so everything the fleet keeps
    # there (plugins, the session file the node probes and collects) is missing.
    capabilities=Capabilities(
        # Claude Code against DeepSeek's endpoint: Claude's session, turn and mode surface.
        runtime=(("api_key", "acp"),),
        sessions=Sessions(assign_id=True, resume="resume", list=True, close=True, additional_dirs=True,
                          start_context=True, list_command=("agents", "--json")),
        turns=Turns(steer=False, interrupt=True),
        requests=Requests(approval=True, user_input="mcp_tool"),
        runtime_modes=_modes(("approval-required", "auto-accept-edits", "full-access", "plan")),
        auth=Auth(kinds=(), key_delivery="helper", key_in_env=False),
        account_layer="omc",
        project_memory_hooks=True,
        usage=Usage(tokens=True, cost=True, rate_limit_reset=False, priced_by_time=True),
        vendor="deepseek",
        api_key_probe_url="https://api.deepseek.com/models",
        model_id_prefixes=("deepseek",),
        console=_CHAT_AND_TERMINAL,
        acp_dialect=_CLAUDE_CODE_ACP,
    ),
    degradations=(
        # Kept under the feasibility escape (architecture §6): DeepSeek publishes concurrency limits only and a
        # prepaid balance, no usage window (https://api-docs.deepseek.com/quick_start/rate_limit and
        # https://api-docs.deepseek.com/api/get-user-balance, fetched 2026-10-25).
        Degradation(
            key="no-quota-reading",
            harness=False,
            capability="Usage window",
            consequence=(
                "No usage window is shown or gated on. Spend is bounded only by the account's runner "
                "cap and the DeepSeek balance."
            ),
        ),
        Degradation(
            key="document-blocks-unsupported",
            harness=False,
            capability="PDF reading",
            consequence=(
                "A PDF is silently dropped: the request succeeds but the model never sees the document, so "
                "it answers without it."
            ),
        ),
        Degradation(
            key="images-flash-only",
            harness=False,
            capability="Images on the top tiers",
            consequence=(
                "The max and high tiers (deepseek-v4-pro) are text only: a screenshot or image is silently "
                "dropped and the model answers without it. Standard and low tiers see images."
            ),
        ),
        Degradation(
            key="prompt-cache-controls-ignored",
            harness=False,
            capability="Prompt cache controls",
            consequence=(
                "Claude's cache controls are ignored. Cache hits depend on DeepSeek's automatic prefix "
                "cache, so cost and latency differ from Claude."
            ),
        ),
        Degradation(
            key="beta-features-ignored",
            harness=False,
            capability="Anthropic beta features",
            consequence="Anthropic beta features the CLI requests have no effect.",
        ),
        Degradation(
            key="web-search-extra-calls",
            harness=False,
            capability="Web search cost",
            consequence=(
                "Each web search is a billed DeepSeek search request, and the results it returns are billed "
                "again as input tokens."
            ),
        ),
        Degradation(
            key="claude-model-names-remapped",
            harness=False,
            capability="Claude model pins",
            consequence="A task's explicit Claude model pin is translated to the nearest DeepSeek tier.",
        ),
        Degradation(
            key="model-quality-differs",
            harness=False,
            capability="Model quality",
            consequence="Results come from DeepSeek models, not Claude, and quality differs.",
        ),
    ),
)


# Every literal below was measured against agy 1.2.11 (linux amd64), in scratch HOMEs with no account
# or, where it says so, with a signed-in scratch session; a field that could not be
# measured says so and takes the fallback whose degradation is published below, so nothing here claims a
# capability not yet built. The ACP runtime is Google's own `agy_acp_server` 1.4.1 (the `[agy-acp-server]` pin),
# measured with a placeholder key and from the Python sources it ships.
ANTIGRAVITY = AgentProvider(
    value="antigravity",
    label="Google Antigravity",
    binary="agy",
    companions=(),
    # The wrapper `runner_cli_pins.py agy-acp-install` puts on the runner's PATH (`agents/antigravity_runner.py`).
    acp_binary="agy-acp-server",
    windows_runner=False,
    macos_runner=False,
    # Its pinned linux-arm64 build installs and answers on arm64 Ubuntu (measured).
    linux_arm64=True,
    # Measured signed in: `retrieveUserQuotaSummary` answers a 5-hour and a weekly window per
    # model group, read by `provider_health._antigravity_identity_and_quota`.
    has_quota_model=True,
    quota_windows=("5h", "7d"),
    # Unlimited: `HOME=<config_dir>` moves the CLI's whole store (`$HOME/.gemini/antigravity-cli/`), and
    # with its keyring branch disabled (`agents/antigravity_keyring.py`) strace shows no write outside the
    # account dir, none into a sibling account's, none under the real home, and no session-bus connect.
    max_accounts_per_node=0,
    # Shared like Codex: `HOME` relocates the whole store, so only the credential file is linked onto the share.
    # The ACP server's own token is written per node from it
    # (`agents/antigravity_acp_auth.py`), never linked: the server saves it by an atomic rename.
    node_pinned=False,
    shared_token_link=True,
    needs_attested_binary=True,
    # The file store's token, the path the CLI opens at start (strace `openat`, agy 1.2.11).
    credential_file=".gemini/antigravity-cli/antigravity-oauth-token",
    # On a terminal session the bridge hands `ask_question` to the question capture, which files one round per
    # question, and the answer is typed into the picker's Write-in row (`agents/antigravity_runner._TUI`, measured
    # live). On ACP the server's question arrives as a permission request the host files as an operator
    # round (`acp_dialect.question_call_prefix`).
    can_interview=True,
    # The CLI names a tool after its step type, lowercased (its shipped hooks doc); measured as the PreToolUse
    # payload's `toolCall.name` on a signed-in terminal.
    ask_tool="ask_question",
    # Measured: `Esc` on the picker records the question as skipped and the turn goes on.
    ask_dismissed_result="(skipped)",
    charter_native=False,
    # Measured: the sign-in prints a Google authorize URL whose redirect is `antigravity.google/oauth-callback`,
    # then prompts "paste the authorization code below".
    login_completion="paste_code",
    # `mcp_<server>_<tool>`, one underscore: agy 1.2.11 spells its built-in server's tools so
    # (`mcp_chrome_devtools_take_screenshot`, read from its binary); the server is registered as `control`
    # (`agy mcp add`). A user server's tools were not seen live (`not-yet-observed`). The settle guard matches it, and
    # the bridge unwraps the lazy `call_mcp_tool {ServerName, ToolName}` form to the same name.
    mcp_tool_prefix="mcp_control_",
    # No moving alias measured: `agy models` lists the live catalogue only once signed in; `--model` exists.
    tier_aliases=(),
    # `CORTEX_STEP_TYPE_INVOKE_SUBAGENT`, its input `{"Subagents": [{TypeName, Role, Prompt, Workspace}]}` (agy
    # 1.2.11 jsonschema). The fleet roster is installed as `<name>.md` agents (`agents/subagents.py`'s antigravity
    # dialect); whether the tool's description lists them was not measured, so no roster is spliced.
    subagent_spawn="the `invoke_subagent` tool, `Subagents[].TypeName` naming the agent (`Role` 2-5 words, `Prompt` the task)",
    subagent_roster="",
    # The CLI's second kind is a Gemini API key (`agents/antigravity_runner.py`). Spillover, as Claude's key:
    # the quota is read, so the key is spent only once it runs out.
    auth_kinds=("oauth", "api_key"),
    api_key_credential_type="gemini_api_key",
    api_key_lane="spillover",
    api_key_helper=False,
    # Spend is gated by the quota reading, as on the other OAuth CLIs, so no per-account cap.
    per_account_runner_cap=False,
    # Off: a new account runs only tasks that ask for this provider until the operator opts it in — a
    # degraded provider should not be picked for ordinary work by default.
    auto_assign_default=False,
    # A task's foreign model id is handed over as written, as on the other OAuth CLIs; nothing reads this
    # field yet, and `agy --model` takes a free-form name (its `--help`).
    foreign_model_ids="passthrough",
    capabilities=Capabilities(
        # Its models are Gemini's, so a Gemini model id attributes here. Both kinds run the ACP server: an API-key
        # session signs in through ACP `authenticate` (the key in `_meta["api-key"]`, nothing written to disk); a
        # signed-in one from the server's own token, written from the CLI's sign-in before launch
        # (`agents/antigravity_acp_auth.py`; the server reads `auth.type` and needs no `authenticate`).
        runtime=(("oauth", "acp"), ("api_key", "acp")),
        # The signed-in launch was never run live (`not-yet-observed`): its switch ships OFF, so a sign-in runs in the
        # terminal until the operator turns ACP on.
        acp_switch_off=("oauth",),
        # `sessionCapabilities.resume` and `list`, and `loadSession`; `session/resume` reattached a session after the
        # server restarted. No `session/close`. No extra directories: the server takes none. Session-start context
        # (the guard brief among it) reaches the model on both runtimes: the ACP host's system prompt and agy's
        # `SessionStart` steps (the node's Antigravity hook bridge).
        sessions=Sessions(assign_id=False, resume="resume", list=True, close=False, additional_dirs=False,
                          start_context=True, list_command=()),
        # `session/cancel` ends the turn `cancelled`. A prompt sent mid-turn was not measured, so no steer.
        turns=Turns(steer=False, interrupt=True),
        # A gated tool call asks through `session/request_permission`, which the host answers by policy; the server's
        # `ask_question` arrives the same way (an `interaction_` call id, no reject option) and becomes an operator
        # round, the nearest the host has to an elicitation.
        requests=Requests(approval=True, user_input="elicitation"),
        # `session/new` reports default/auto_edit/yolo.
        runtime_modes=_modes(("approval-required", "auto-accept-edits", "full-access")),
        auth=Auth(kinds=(), key_delivery="acp_authenticate", key_in_env=False),
        account_layer="none",
        # The memory rows reach agy through the per-run hook file: tool rows via the bridge, lifecycle rows via the
        # ACP host (`acp/lifecycle.py`) or the bridge's derived slots on the terminal
        # (`agents/antigravity_runner.py::_HARNESS_EVENTS`).
        project_memory_hooks=True,
        # The server's only usage report is a `usage_update` of the context window (`used`/`size`): no token counts
        # and no cost (`no-spend-reading`).
        usage=Usage(tokens=False, cost=False, rate_limit_reset=False, priced_by_time=False),
        vendor="google",
        api_key_probe_url="https://generativelanguage.googleapis.com/v1beta/models",
        model_id_prefixes=("gemini",),
        console=_CHAT_AND_TERMINAL,
        # The server writes a failed turn as agent text, then `end_turn` (its `api_errors`/`quota_errors` modules):
        # Google's own message for a refused key, its quota sentence, and the fallback it prefixes every other
        # execution error with, plus its signed-out, onboarding, MCP-start and lost-connection texts (read from
        # its source). The charter rides `_meta.antigravity.systemPrompt.append`, which `initialize` advertises in
        # its own `_meta` rather than as `sessionCapabilities.systemPrompt`. Its `ask_question` reaches the host as
        # a permission request whose call id starts `interaction_`.
        acp_dialect=AcpDialect(
            # Google retiring the client first: it arrives as `Onboarding failed: …`, which `auth_expired` would claim.
            error_text_patterns=((r"(?:Onboarding failed: )?This client is no longer supported for Gemini Code Assist",
                                  "client_unsupported"),
                                 (r"API key not valid|Logged out\. Sign in again|Onboarding failed", "auth_expired"),
                                 (r"Usage Limit Reached", "usage_limit"),
                                 # A prepaid Gemini API key out of credit: Google's own sentence inside the
                                 # server's error prefix (a 429 `RESOURCE_EXHAUSTED`), money on its first
                                 # occurrence rather than the generic `api_error` below.
                                 (r"(?:Agent execution error: )?\[API Error: [^\n]*?Your prepayment credits are "
                                  r"depleted", "credit_exhausted"),
                                 (r"Agent execution error: |\[API Error: |The MCP server '[^']*' failed to initialize|"
                                  r"Agent connection was lost", "api_error")),
            # What follows its `api_error` prefixes is Google's own error (a 429 `RESOURCE_EXHAUSTED`, a 5xx): the
            # vendor classifier names it.
            vendor_error_text=True,
            system_prompt_meta=("antigravity", "systemPrompt", "append"),
            question_call_prefix="interaction_",
            # The server runs no `SessionStart`, prompt-submit, session-end or compaction hook and fires `Stop` only
            # at session end, so the host runs all five (`acp/lifecycle.py`); and it ignores every `injectSteps`, so
            # hook context reaches the model at the next prompt.
            host_lifecycle=("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PreCompact"),
            context_channel="turn_boundary",
        ),
        health_probe="code_assist",
        # A prefix, pinned through the CLI's own public OAuth client id: the rest (PKCE challenge, state) is fresh per
        # sign-in, and a prefix ending at `auth?` would admit a link carrying any other client's id. Measured on every
        # agy 1.2.11 sign-in (`agents/antigravity.py::_AUTH_URL_RE` is the node's copy).
        authorize_url_anchor=(
            "https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id="
            "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com&",
            False,
        ),
    ),
    degradations=(
        Degradation(
            key="not-yet-observed",
            harness=True,
            capability="Unconfirmed on a live run",
            consequence=(
                "Built from the CLI and server binaries but not yet seen working on a live run: the push, "
                "pull-request, tmux, stash, dangerous-delete and settle guards and the turn-end test gate; hook "
                "guidance and the rule list at session start; the installed reviewer, verifier and recon agents; "
                "API-error recovery; ACP questions to the operator; a signed-in session on ACP (its switch ships "
                "off); and a Shared sign-in used on another machine. Until a live run confirms them, treat these "
                "runners as less closely governed."
            ),
        ),
        Degradation(
            key="home-is-account-dir",
            harness=False,
            capability="Runner home directory",
            consequence=(
                "Commands a runner runs see the account's own directory as their home, so settings kept in "
                "the machine user's home — shell profile, global git settings, command-line sign-ins — do "
                "not apply to them."
            ),
        ),
        Degradation(
            key="microvm-signed-in-legacy-runtime",
            harness=False,
            capability="Signed-in sessions on MicroVM nodes",
            consequence=(
                "A signed-in Antigravity session on a MicroVM node runs the legacy terminal runtime: its ACP "
                "server's token is written from the sign-in on the node, as the account's own user, which the "
                "VM's copy of the account cannot take. API-key sessions on MicroVM nodes, and every session on "
                "static nodes, run on ACP."
            ),
        ),
        # Kept under the feasibility escape (architecture §6): agy-acp-server 1.4.1 sends no token counts or
        # cost (measured), and no agy
        # 1.2.11 transcript line carries them (`agents/antigravity_transcript.py`).
        Degradation(
            key="no-spend-reading",
            harness=False,
            capability="Run cost",
            consequence=(
                "Runs on this provider report no token counts or cost, so this account's spend is not "
                "shown anywhere."
            ),
        ),
    ),
)


# Pi (earendil-works/pi) driven over ACP by `pi-acp`, on the org's Anthropic API key (architecture C12, A9). Every
# literal below was read off pi 1.0.2 and pi-acp 0.0.34, the pins in `packaging/platform-pins.toml`, and
# measured against a live run where it says so (`agents/pi.py`). It has no legacy runtime: a launch is the ACP host.
PI = AgentProvider(
    value="pi",
    label="Pi",
    # The ACP agent the host spawns (it spawns `pi` itself): what a node must attest before it takes a Pi run.
    binary="pi-acp",
    # The package's postinst installs `pi` with `|| true`, so a node can attest `pi-acp` and still lack it.
    companions=("pi",),
    acp_binary="",
    windows_runner=False,
    macos_runner=False,
    linux_arm64=True,
    # A key spends against the Anthropic balance, not a usage window: nothing to gate on.
    has_quota_model=False,
    quota_windows=(),
    max_accounts_per_node=0,
    # An API-key account: nothing is signed in on a node, so nothing is pinned or linked onto the share.
    node_pinned=False,
    shared_token_link=False,
    # Not on every node by construction: the npm arms install `pi-acp` and its companion `pi` (the package's postinst).
    needs_attested_binary=True,
    credential_file="",
    can_interview=True,
    # The fleet extension's question tool (`agents/pi_extension/jaah-harness.ts`): each question is a Pi `select`,
    # which pi-acp asks as a permission request the host files as an operator round (`acp_dialect.question_call_prefix`);
    # the answers return as the tool's result.
    ask_tool="ask_user_question",
    # The extension's answer for a question whose round chose none of its options.
    ask_dismissed_result="(no option chosen)",
    charter_native=False,
    # No login: the account is its API key.
    login_completion="",
    # Pi names a server's tools `mcp__<server>__<tool>`; the server is declared with `direct` exposure (`agents/pi.py`).
    mcp_tool_prefix="mcp__control__",
    # Pi's `--model` takes a fuzzy model name, so Claude's moving aliases resolve on the Anthropic provider.
    tier_aliases=(("max", "opus"), ("high", "opus"), ("standard", "sonnet"), ("low", "haiku")),
    # Pi ships no subagents.
    subagent_spawn="nothing: Pi has no subagents, so do that work yourself",
    subagent_roster="",
    auth_kinds=("api_key",),
    # A9: Pi runs on the org's Anthropic API key, the credential type Claude's key accounts point at.
    api_key_credential_type="anthropic_api_key",
    # Its only kind is a key, so its accounts are ordinary picks, as DeepSeek's are.
    api_key_lane="peer",
    # The key comes from a helper command (Pi's `auth.json` `!command`), never the environment.
    api_key_helper=True,
    # With no quota to gate on, the operator's per-account cap is what bounds spend.
    per_account_runner_cap=True,
    # Off: a new Pi account runs only the tasks that ask for it until the operator opts it in.
    auto_assign_default=False,
    foreign_model_ids="passthrough",
    capabilities=Capabilities(
        # No terminal runtime to switch to: the switch is not offered (`app/acp_runtime.py::ACP_ONLY`).
        runtime=(("api_key", "acp_only"),),
        # pi-acp names the session, lists and loads its sessions (`session/list`, `session/load`); no close.
        sessions=Sessions(assign_id=False, resume="load", list=True, close=False, additional_dirs=False,
                          start_context=True, list_command=()),
        # A prompt sent mid-turn is queued behind the running one, not steered into it; cancel aborts the turn.
        turns=Turns(steer=False, interrupt=True),
        # Pi never asks before a tool call; it asks the operator through the fleet extension's tool (`ask_tool`).
        requests=Requests(approval=False, user_input="mcp_tool"),
        runtime_modes=_modes(("full-access",)),
        auth=Auth(kinds=(), key_delivery="helper", key_in_env=False),
        account_layer="none",
        # The tool rows run in the fleet extension through the dispatcher, the lifecycle rows in the ACP host.
        project_memory_hooks=True,
        # Through the fork in `packages/acp-forks/pi-acp/`: each `usage_update` carries pi's running cost, the
        # prompt result its turn's tokens per model in `_meta.quota` (`acp_dialect`), and a usage limit its reset.
        usage=Usage(tokens=True, cost=True, rate_limit_reset=True, priced_by_time=False),
        # Prompts and the key go to Anthropic; Pi's own network activity is switched off (`agents/pi.py::QUIET_ENV`).
        vendor="anthropic",
        # The key is the Anthropic key the Credentials page's own tester checks, as Claude's.
        api_key_probe_url=None,
        # Its models are Claude's, so a Claude model id attributes to Claude.
        model_id_prefixes=(),
        # The chat view is every ACP session's; Pi draws no terminal UI, so no console session opens on it.
        console=Console(chat_view=True, terminal_view=False, toggle=False,
                        reason="Pi has no terminal UI: its runs show in the chat view."),
        health_probe="broker_key",
        # pi-acp runs no lifecycle hook, so the host runs them (`acp/lifecycle.py`); the tool and compaction events
        # run in-band in the fleet extension `agents/pi_rpc.py` loads, the hook context joining the tool result.
        # The charter is the host's `system-prompt.md`, which `pi_rpc.py` appends (`--append-system-prompt`); a
        # `select` the extension's question tool raises reaches the host as a permission request whose call id
        # starts `pi-ui-` (pi-acp 0.0.34 `extensionUiToolCall`).
        acp_dialect=AcpDialect(usage_meta=_QUOTA_USAGE, question_call_prefix="pi-ui-",
                               host_lifecycle=("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"),
                               system_prompt_file_env="JAAH_PI_SYSTEM_PROMPT"),
    ),
    degradations=(
        Degradation(
            key="acp-runtime-only",
            harness=False,
            capability="Terminal runtime",
            consequence=(
                "Pi runs only on the ACP runtime: it has no runtime switch, and no interactive console session "
                "opens on a Pi account; its runs show in the chat view."
            ),
        ),
        Degradation(
            key="no-subagents",
            harness=True,
            capability="Subagents",
            consequence=(
                "Pi has no subagents: a runner does review and verification itself, so they are not separate "
                "lanes."
            ),
        ),
        # Kept under the feasibility escape (architecture §6): the Anthropic API exposes a monthly spend cap and
        # per-minute buckets, not a usage window, and reading them needs an Admin key
        # (https://platform.claude.com/docs/en/api/rate-limits, fetched 2026-10-25).
        Degradation(
            key="no-quota-reading",
            harness=False,
            capability="Usage window",
            consequence=(
                "No usage window is shown or gated on. Spend is bounded only by the account's runner cap and "
                "the Anthropic balance."
            ),
        ),
    ),
)


# Claude first: the Add Account dialog renders this order, and Claude is the default.
PROVIDERS: tuple[AgentProvider, ...] = (CLAUDE, CODEX, DEEPSEEK, ANTIGRAVITY, PI)

PROVIDER_VALUES: tuple[str, ...] = tuple(p.value for p in PROVIDERS)

# No "unset" member — absence is not a state; NOT NULL with this default.
DEFAULT_PROVIDER = "claude"
# The two `login_completion` values. The records above keep literal strings on purpose: the catalogue test
# reads them with `ast.literal_eval`, which cannot evaluate a Name;
# a test keeps literals and constants aligned.
PASTE_CODE = "paste_code"
AWAIT_CREDENTIALS = "await_credentials"


def by_value(value: str) -> AgentProvider | None:
    """The catalogue row for a wire/DB `provider` value, or None for a value this build does not know.

    Never a fallback to `CLAUDE`: the caller must be able to tell (refuse it, or a newer build's row);
    resolving to Claude would run a Claude login for another provider's account."""
    for provider in PROVIDERS:
        if provider.value == value:
            return provider
    return None


def capabilities_of(value: str) -> Capabilities | None:
    """`value`'s capability groups, or None for a value this build does not know."""
    provider = by_value(value)
    return provider.capabilities if provider is not None else None


def dialect_for(value: str) -> AcpDialect:
    """`value`'s ACP dialect; plain ACP (the empty dialect) for a value this build does not know."""
    caps = capabilities_of(value)
    return caps.acp_dialect if caps is not None else AcpDialect()


def account_layer(value: str) -> str:
    """The account layer `value`'s homes carry (`Capabilities.account_layer`): "omc" for a CLI that is Claude
    Code itself (Claude, DeepSeek), so every Claude Code mechanism applies unchanged. "none" for an unknown
    value: an unrecognised provider never borrows another CLI's path."""
    caps = capabilities_of(value)
    return caps.account_layer if caps is not None else "none"


def health_probe(value: str) -> str:
    """How the node reads `value`'s accounts (`Capabilities.health_probe`); "none" for an unknown value."""
    caps = capabilities_of(value)
    return caps.health_probe if caps is not None else "none"


def session_list_argv(value: str) -> tuple[str, ...]:
    """The argv that prints `value`'s live sessions in one config dir as a JSON array (`binary` plus
    `Capabilities.sessions.list_command`), or `()` for a CLI with no listing or a value this build does not
    know."""
    provider = by_value(value)
    if provider is None or not provider.capabilities.sessions.list_command:
        return ()
    return (provider.binary, *provider.capabilities.sessions.list_command)


def implied_on_wire(value: str) -> bool:
    """True when a wire payload may OMIT `provider` for `value`: an absent `provider` reads as
    `DEFAULT_PROVIDER` on both sides, so omitting it keeps the payload byte-identical for an older peer. A
    wire default, not a capability: the senders ask this, never compare against `DEFAULT_PROVIDER`."""
    return value == DEFAULT_PROVIDER


# The config-snapshot key an upgraded daemon publishes, and the value that means "speaks the current names".
NAMING_SNAPSHOT_KEY = "naming"
CURRENT_NAMING = "jaah"
# The key a node publishes to say it exports the `JAAH_*` runner variables. Every node now exports only those
# (rename wave R1); a control plane of the previous release still reads it to choose them.
ENV_NAMING_SNAPSHOT_KEY = "env_naming"
CURRENT_ENV_NAMING = "jaah"
# The agent verbs, renamed to industry terms — `(current, pre-rename)`, the verb table's
# `agent_vocabulary.ALIASES` read from here. A verb `a.b_c` is also the MCP tool `a_b_c`, so one pair covers
# both spellings. An upgraded daemon accepts both and publishes `vocabulary: industry`; for any other node
# the control plane composes the pre-rename names (`dispatch_compose.placeholders.respell_vocabulary`), which every
# node serves. Deleted with the aliases (D5).
VOCABULARY_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("task.complete", "task.settle"),
    ("input.request", "task.question"),
    ("input.request_batch", "agent.ask"),
    ("input.responses", "agent.answers"),
    ("artifact.upload", "task.artifact"),
    ("artifact.upload_begin", "task.artifact_ticket"),
    ("artifact.upload_commit", "task.artifact_confirm"),
    ("artifact.read", "task.read_artifact"),
    ("notification.send", "agent.notify"),
    ("credential.list", "task.credential_list"),
    ("credential.get", "task.credential_get"),
    ("task.get_context", "agent.task"),
    ("task.create_follow_up", "task.follow_up"),
    ("task.create_batch", "task.append_tasks"),
    ("task.update", "task.edit_filed"),
    ("task.delete", "task.delete_filed"),
    ("pull_request.enqueue", "task.pr_queue"),
    ("pull_request.observed", "task.pr_observed"),
    ("schedule.create", "task.cron_create"),
    ("schedule.update", "task.cron_update"),
    ("schedule.delete", "task.cron_delete"),
    ("trigger.create", "task.trigger_create"),
    ("trigger.update", "task.trigger_update"),
    ("trigger.delete", "task.trigger_delete"),
    ("project.get", "task.project_get"),
    ("project.update", "task.project_update"),
    ("project.env_set", "task.project_env_write"),
    ("project.env_delete", "task.project_env_delete"),
    ("session.heartbeat", "task.heartbeat"),
    ("session.start", "task.claim_active"),
    ("session.error", "task.runner_break"),
    ("session.close", "task.console_exit"),
    ("session.resume", "task.console_resume"),
    ("merge_queue.batch", "lane.batch"),
    ("merge_queue.queue", "lane.queue"),
    ("merge_queue.event", "lane.event"),
    ("merge_queue.base_check", "lane.base_check"),
)
VOCABULARY_SNAPSHOT_KEY = "vocabulary"
CURRENT_VOCABULARY = "industry"

