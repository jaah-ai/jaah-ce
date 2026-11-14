"""Which personas the agent router may choose a runner for. Every other persona's decision record falls back
(`persona_not_routed`)."""
from __future__ import annotations

#: The blind review children of a coder task (architecture §4.6).
REVIEW_PERSONAS: tuple[str, ...] = ("reviewer", "security_reviewer", "tester")
ROUTED_PERSONAS: tuple[str, ...] = ("coder",) + REVIEW_PERSONAS
