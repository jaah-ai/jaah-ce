"""The role table (`_ROLES`, keyed by charter key, in table order) and its readers."""
from __future__ import annotations

from app.dispatch_compose.doctrine import BUNDLE
from app.persona_registry import MODE_ANALYSIS, charter_mode

# ── Per-role prose table ──
# Per-role inputs/focus/gate/output are this edition's doctrine bundle's (`doctrine.BUNDLE.roles`), rendered by
# `role_render._render_role_section`, keyed by `charter_key`. Mode, delivery, terminals and manifest are persona
# properties on `app/persona_registry.py`. `charter_keys()` returns the bundle's key order.
_ROLES: dict[str, dict[str, str]] = BUNDLE.roles  # type: ignore[assignment]  # a bundle row is a role row


# The analysis roles that get the read-only doctrine (`_ANALYSIS_PROHIBITIONS`, via their `extra_rules`);
# derived from the registry's `mode` over this table's keys.
_ANALYSIS_CHARTERS = frozenset(k for k in _ROLES if charter_mode(k) == MODE_ANALYSIS)


def role_prose(charter_key: str) -> dict[str, str]:
    """One role's prose row, by charter key, for a reader outside the composer (`support_kb/doctrine.py`
    renders it for the knowledge base). Raises KeyError for a key no role carries."""
    return _ROLES[charter_key]


def charter_keys() -> tuple[str, ...]:
    """Every charter key a role is written for, in table order."""
    return tuple(_ROLES)
