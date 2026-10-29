"""Behaviour tables the composer and the console read off a charter key, and the edition-neutral helpers both
doctrine bundles share (`dispatch_compose/doctrine.py`). No role prose: that is each bundle's."""
from __future__ import annotations

from app.persona_registry import TASK_CREATION_LICENCES


def _licence_bound(persona: str) -> str:
    """The persona's task-creation constraint, verbatim as the server quotes it on a 403."""
    return f"The server enforces this bound: \"{TASK_CREATION_LICENCES[persona].constraint}\".\n"


# Roles whose gate never asks, whatever the autonomous switch says. Keyed on role, not `delivery` (the
# Planner shares `"db"` with interviewing roles). See `_autonomous_directive`'s `asks_at_gate`.
_NEVER_ASKS: frozenset[str] = frozenset({"planner"})

# Roles whose operator is live at the Web Console: they get `_CONSOLE_DIRECTIVE` (ask in prose) instead
# of `_INTERVIEW_DIRECTIVE`'s parking AskUserQuestion rounds. See `_autonomous_directive`'s `console_present`.
_CONSOLE_INTERVIEW: frozenset[str] = frozenset({"advisor"})

# Roles whose runner must never be reaped as idle; surfaced on the verdict as `autoclose`, which
# `app/lease_drain.py` turns into `VISIBLE_AUTOCLOSE`. A conversing role parks at the prompt between
# messages, which the idle reaper would otherwise kill.
_NO_AUTOCLOSE: frozenset[str] = frozenset({"advisor"})


#: The `header` values a Full Auto question must carry, one per human-only blocker kind. Mirrored by the
#: node-side AskUserQuestion capture hook, which refuses any other question on a Full Auto task (a parity test
#: pins the two).
FULL_AUTO_BLOCKER_HEADERS: tuple[str, ...] = ("Credential", "Access", "Human act", "External", "Guard")


# The onboard brief's gate fragments; `build_system_prompt_with` swaps each for its blank-gate twin when
# either gate is blank.
_ONBOARD_GATES_ROW = "quick gate `{gate_quick_cmd}`, full gate `{gate_full_cmd}`"
_ONBOARD_GATES_TOOL = (
    "- `Bash` for the gates: `{gate_quick_cmd}` "
    "as your loop, `{gate_full_cmd}` green before the PR. If they do not exist yet, create them at "
    "exactly those names.\n"
)
_ONBOARD_GATES_DELIVERABLE = "`{gate_quick_cmd}` and `{gate_full_cmd}` existing"
_GATE_ABSENT_REWRITES: tuple[tuple[str, str], ...] = (
    (_ONBOARD_GATES_ROW,
     "and the two gate columns — at least one BLANK (`project_get` shows both)"),
    (_ONBOARD_GATES_TOOL,
     "- `Bash` for the gates. At least one gate column is blank; creating that gate is part of your work "
     "(\"none declared — skip it\" never means leave it unbuilt). Create a set column's command at exactly "
     "that name. For a blank one, write a wrapper over the repo's existing checks (e.g. `./dc check quick` / "
     "`./dc check full`), then set BOTH columns with `project_update` and record that you did.\n"),
    (_ONBOARD_GATES_DELIVERABLE,
     "a quick and a full gate existing, both written to the project's gate columns"),
)


def _folder_rewritten(value: object, rewrites: tuple[tuple[str, str], ...]) -> object:
    """`value` with every fragment of `rewrites` swapped for its folder twin; a non-string role value (a
    playbook role's flag) passes through unchanged."""
    if not isinstance(value, str):
        return value
    for fragment, folder in rewrites:
        value = value.replace(fragment, folder)
    return value
