"""Rendering one role's prose into the system prompt's "Your Role" section."""
from __future__ import annotations

from app.dispatch_compose.doctrine import BUNDLE
from app.dispatch_compose.role_tables import _folder_rewritten
from app.dispatch_compose.settle_terms import SettleTerms, _settle_tool_for, settle_terms_of
from app.persona_registry import DELIVERY_OUTCOME, DELIVERY_PR, MODE_BUILD, charter_delivery, charter_mode

def _waits_for_a_merge(role: dict[str, str], *, mode: str) -> bool:
    """Whether this role can open a PR (a build role, which reads the Engineering rules) and has a terminal
    call to time against its merge.

    The one filter shared by `_WAIT_FOR_PR_MERGE` and `_BOUNDED_GATE_WAIT` (which refers to it), so the
    two can never reach different roles; a build role it excludes has `_MERGE_WAIT_POINTER` dropped."""
    return mode == MODE_BUILD and not role.get("no_terminal_call")


def _render_role_section(role: dict[str, str], *, charter_key: str, persona: str) -> str:
    """Render one role's prose (`_ROLES[charter_key]`) into the "Your Role" section after the charter.

    `charter_key` reaches `charter_mode()` / `charter_delivery()`. `persona` is required rather than
    derived: `PERSONA_MANIFEST` and `PERSONA_TERMINALS` key on it and `PERSONA_CHARTER` is not injective."""
    return render_role_section_with(
        role,
        mode=charter_mode(charter_key),
        delivery=charter_delivery(charter_key),
        terms=settle_terms_of(persona),
        # The repository shape: a folder's role section is composed only through `build_system_prompt_with`,
        # which applies `_FOLDER_REWRITES` first.
        repo=True,
    )


def render_role_section_with(
    role: dict[str, str], *, mode: str, delivery: str, terms: SettleTerms, repo: bool
) -> str:
    """Render `role`'s prose into the "Your Role" section after the charter, from data alone.

    Renders only the role's own deltas; shared doctrine lives in runner-charter.md. Takes no per-dispatch
    value: `_WAIT_FOR_PR_MERGE` renders for build roles without `no_terminal_call`."""
    waits = repo and _waits_for_a_merge(role, mode=mode)
    parts = [f"\n---\n# Your Role: {role['title']} (mode={mode})\n"]
    if role.get("inputs"):
        parts.append(f"\n**Inputs:** {role['inputs']}\n")
    parts.append(f"\n**Focus:** {role['focus']}\n")
    if role.get("output_contract"):
        parts.append(f"\n**Output contract:** {role['output_contract']}\n")
    if role.get("input_slots"):
        parts.append(f"\n### Inputs you receive\n{role['input_slots']}\n")
    if role.get("skills"):
        parts.append(f"\n### Skills to use\n"
                     f"{role['skills']}\n")
    if role.get("tools"):
        parts.append(f"\n### Tools to use\n{role['tools']}\n")
    if role.get("gate"):
        parts.append(f"\n## Gate: {role['gate_name']}\n{role['gate']}\n")
    if role.get("extra_rules"):
        # A pointer to a block this role does not get would dangle.
        rules = role["extra_rules"] if waits else role["extra_rules"].replace(BUNDLE.shared["_MERGE_WAIT_POINTER"], "")
        parts.append(f"\n{rules}\n")
    # A build role on a folder slot gets the folder-delivery contract instead of its engineering rules.
    if not repo and mode == MODE_BUILD and not role.get("no_terminal_call"):
        parts.append(f"\n{BUNDLE.shared['_FOLDER_DELIVERY']}")
    # One shared copy for every build role that has a terminal call.
    if waits:
        wait = BUNDLE.shared["_WAIT_FOR_PR_MERGE"]
        if delivery != DELIVERY_PR:
            wait = _folder_rewritten(wait, BUNDLE.rewrites["_OUTCOME_MERGE_WAIT_REWRITES"])
        parts.append(f"\n{wait}")
    if role.get("tool"):
        endpoint = role.get("structured_endpoint", "")
        # The Planner overrides `tool_heading`: its append is the first of two writes (settle is the second).
        heading = role.get("tool_heading", "the one sanctioned write beside your settle")
        parts.append(f"\n## Your tool — {heading} ({endpoint})\n{role['tool']}\n")
    # Every role that settles gets its persona's manifest and terminal types; `no_terminal_call` roles do not.
    if not role.get("no_terminal_call"):
        parts.append(
            "\n## Complete this task — the ONE terminal call\n"
            + _settle_tool_for(
                terms,
                summary_hint=role.get("settle_summary_hint", ""),
                gate_arg=role.get("settle_gate_arg", ""),
            )
            + "\n"
        )
    parts.append(BUNDLE.shared["_RENDERED_REPORT_MANDATE"])
    section = "".join(parts)
    if delivery == DELIVERY_OUTCOME:
        section = _folder_rewritten(section, BUNDLE.rewrites["_OUTCOME_SETTLE_REWRITES"])
    return section
