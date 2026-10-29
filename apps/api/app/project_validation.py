"""Pure-function validators for `Project` fields the DB layer can't constrain itself.

`Project.tech_stack` (app/models/) is a JSONB list of {"layer", "technology"} objects — Postgres
can't enforce "no duplicate (layer, technology) pair" on a JSONB column, so that check lives here
in the application layer instead. No I/O: callers (future CRUD routes, tests) own deciding what to
do with a `True` result (reject with a 400/422, etc.).
"""
from __future__ import annotations

__all__ = ["has_duplicate_tech_stack_entry"]


def has_duplicate_tech_stack_entry(tech_stack: list[dict[str, str]]) -> bool:
    """True if any two entries share the same (layer, technology) pair.

    Comparison is case-sensitive and exact-match on both fields — "Frontend"/"React" and
    "frontend"/"React" are treated as distinct entries, matching how the fields are stored.
    """
    seen: set[tuple[str, str]] = set()
    for entry in tech_stack:
        key = (entry.get("layer", ""), entry.get("technology", ""))
        if key in seen:
            return True
        seen.add(key)
    return False
