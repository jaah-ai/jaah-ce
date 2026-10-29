"""agents — the runner adapter registry: one lookup, every provider, no default.

`get_adapter(provider)` is the ONLY way a launch path may obtain a CLI's argv, environment, hook rendering
or transcript reader. There is no fallback: an unregistered provider raises `UnknownProvider`, never
another vendor's adapter (`tests/test_agent_adapters.py` proves it). Keys are `accounts.provider` values
(`agent_providers.PROVIDER_VALUES`); a catalogued provider with no adapter is simply absent here.
"""
from __future__ import annotations

from . import login as _login
from .antigravity import AntigravityLoginAdapter
from .antigravity_runner import AntigravityAdapter
from .base import (
    ADAPTER_MEMBERS,
    AcpLaunch,
    AcpLaunchContext,
    AgentAdapter,
    ComposerTui,
    ConsentDialog,
    LifecycleChains,
    QuitSignature,
    SESSION_ASSIGN,
    SESSION_DISCOVER,
    TASK_ARGV_FLAG,
    TASK_ARGV_POSITIONAL,
    TASK_TMUX_PASTE,
    TokenCounts,
    TurnError,
)
from .claude import ClaudeAdapter
from .claude_login import ClaudeLoginAdapter
from .codex import CodexAdapter
from .codex_login import CodexLoginAdapter
from .deepseek import DeepSeekAdapter
from .login import LOGIN_ADAPTERS, LoginAdapter, login_adapter_for
from .pi import PiAdapter


class UnknownProvider(KeyError):
    """No adapter is registered for this `accounts.provider` value.

    A `KeyError` subclass so existing dict-lookup guards keep working. Raised, never swallowed into a default."""


# provider value -> the one instance serving it. Adapters are stateless, so one instance each.
_ADAPTERS: dict[str, AgentAdapter] = {
    ClaudeAdapter.provider: ClaudeAdapter(),
    CodexAdapter.provider: CodexAdapter(),
    AntigravityAdapter.provider: AntigravityAdapter(),
    # Claude Code on DeepSeek's endpoint: Claude's adapter plus an environment (`agents/deepseek.py`).
    DeepSeekAdapter.provider: DeepSeekAdapter(),
    # Pi over ACP only; an API-key account, so no LOGIN row (`agents/pi.py`).
    PiAdapter.provider: PiAdapter(),
}

# ── the LOGIN registry's rows ───────────────────────────────────────────────────────────────────────
#
# Declared in `login.py`, filled here: the seam must not import its implementations. Mutated, never
# rebound — `login.login_adapter_for` reads the original dict object. Claude Code's row is finished by the
# login machine's own steps (`machine_finished`, `agents/claude_login.py`).
LOGIN_ADAPTERS[ClaudeLoginAdapter.provider] = ClaudeLoginAdapter()
LOGIN_ADAPTERS[CodexLoginAdapter.provider] = CodexLoginAdapter()
# Registered in the SAME change as its runner adapter (Architecture A2): an unbound task may pick any
# healthy account, so an antigravity account must never exist before a runner can launch on it.
LOGIN_ADAPTERS[AntigravityLoginAdapter.provider] = AntigravityLoginAdapter()


def get_adapter(provider: str) -> AgentAdapter:
    """The adapter for `provider`, or `UnknownProvider`.

    The error names what is registered, so a missing adapter and a typo read differently in a journal."""
    try:
        return _ADAPTERS[provider]
    except KeyError:
        raise UnknownProvider(
            f"no runner adapter is registered for provider {provider!r} on this node "
            f"(registered: {', '.join(sorted(_ADAPTERS)) or 'none'})"
        ) from None


def registered_providers() -> tuple[str, ...]:
    """Every provider this node can launch, sorted — for tests and reports, never a fallback list."""
    return tuple(sorted(_ADAPTERS))


__all__ = [
    "AcpLaunch", "AcpLaunchContext",
    "AgentAdapter", "ComposerTui", "ConsentDialog", "LifecycleChains", "QuitSignature", "TokenCounts", "TurnError",
    "UnknownProvider",
    "ADAPTER_MEMBERS",
    "get_adapter", "registered_providers",
    "AntigravityAdapter", "DeepSeekAdapter", "PiAdapter",
    "LoginAdapter", "LOGIN_ADAPTERS", "login_adapter_for", "ClaudeLoginAdapter",
    "AntigravityLoginAdapter",
    "TASK_ARGV_POSITIONAL", "TASK_ARGV_FLAG", "TASK_TMUX_PASTE",
    "SESSION_ASSIGN", "SESSION_DISCOVER",
]
