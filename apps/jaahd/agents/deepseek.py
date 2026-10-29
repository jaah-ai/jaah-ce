"""agents/deepseek.py — the DeepSeek adapter: Claude Code itself, pointed at DeepSeek's endpoint.

Everything that makes a runner a DeepSeek runner is the environment below; argv, hooks, `--settings`, the
plugin layer, the charter, `AskUserQuestion` and the transcript fold are Claude's, inherited whole
(`agent_providers.DEEPSEEK.binary == "claude"`). The key is NOT here: it rides the inherited
`api_key_settings` → `apiKeyHelper` (`fleet_dispatch.api_key_helper_command`), so no key variable is ever
exported. The variables are emitted per runner (`fleet_dispatch.build_auth_lines`) or per console tab
(`clauded_wt.py`), never node-wide: a variable set on the tmux server reaches every pane.

Pinned by `tests/golden/deepseek_launch.sh`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .base import adapter_env
from .claude import ClaudeAdapter

# DeepSeek's Anthropic-compatible endpoint (api-docs.deepseek.com/quick_start/agent_integrations/claude_code).
BASE_URL = "https://api.deepseek.com/anthropic"

# Every CLI alias is remapped, so none resolves to a `claude-*` id: an unmapped alias is sent as Claude's
# own model id, which DeepSeek silently serves as flash (measured with `--model fable`). The `[1m]` suffix
# gives the 1M-token window, so `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is deliberately not set. Subagents and the
# small-model (haiku-class) calls run flash; effort is always max (operator decision).
RUN_ENV: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_BASE_URL", BASE_URL),
    ("ANTHROPIC_DEFAULT_FABLE_MODEL", "deepseek-v4-pro[1m]"),
    ("ANTHROPIC_DEFAULT_OPUS_MODEL", "deepseek-v4-pro[1m]"),
    ("ANTHROPIC_DEFAULT_SONNET_MODEL", "deepseek-flash[1m]"),
    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "deepseek-flash"),
    ("CLAUDE_CODE_SUBAGENT_MODEL", "deepseek-flash"),
    ("CLAUDE_CODE_EFFORT_LEVEL", "max"),
    ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
)

# DeepSeek's peak-price window — the ONE home of it: UTC, Monday-Friday, [01:00,04:00) and [06:00,10:00).
# A usage line spent inside it is billed at the `@peak` rate (`apps/api/app/provider_model_prices.py`).
PEAK_TIER = "@peak"
_PEAK_WEEKDAYS = frozenset(range(5))  # Monday=0 .. Friday=4
_PEAK_HOURS: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def is_peak(stamp: datetime) -> bool:
    """Whether `stamp` falls in DeepSeek's peak window. A naive stamp is read as UTC."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    utc = stamp.astimezone(timezone.utc)
    return utc.weekday() in _PEAK_WEEKDAYS and any(lo <= utc.hour < hi for lo, hi in _PEAK_HOURS)


def line_pricing_tier(obj: dict) -> str:
    """`"@peak"` when this transcript line's own `timestamp` is in the peak window, else `""`.

    A line with no readable timestamp reads off-peak: the fold never guesses a surcharge."""
    raw = obj.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        return PEAK_TIER if is_peak(datetime.fromisoformat(raw.replace("Z", "+00:00"))) else ""
    except (ValueError, OverflowError, TypeError):
        # A runner writes this file: an out-of-range stamp (`0001-01-01T00:00:00+01:00` overflows the UTC
        # shift) costs this line its tier, never the capture.
        return ""


class DeepSeekAdapter(ClaudeAdapter):
    """Claude Code on DeepSeek. Stateless — the registry holds one instance."""

    provider = "deepseek"

    def env(self, *, config_dir: str, worktree: str = "") -> dict[str, str]:
        """Claude's account env plus DeepSeek's endpoint, models and effort.

        RUN-scoped (`adapter_env`), so kept on an API-key dispatch with no `config_dir`. No
        `ANTHROPIC_AUTH_TOKEN` and no `ANTHROPIC_API_KEY`: the key is served by `apiKeyHelper`."""
        return adapter_env(config_dir, run={**super().env(config_dir=config_dir, worktree=worktree), **dict(RUN_ENV)})

    def line_pricing_tier(self, obj: dict) -> str:
        """The per-line tier callback `session_sweep.Analytics` takes for this provider's transcripts."""
        return line_pricing_tier(obj)
