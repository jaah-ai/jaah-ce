"""The settle section a role renders: terminal types, manifest and the one terminal call, per persona."""
from __future__ import annotations

from dataclasses import dataclass

from app.persona_registry import PERSONA_MANIFEST, PERSONA_SETTLE_STATES, PERSONA_TERMINALS
from app.task_lifecycle import AWAITING_RESPONSE
from app.task_manifest import ARTIFACT_KIND_LABELS, KIND_MEDIA, OUTCOME_SUMMARY_TARGET_CHARS, REQUIRED_HARD
from app.task_settle import FOLDER_BUILD_TERMINALS, TERMINAL_REFERENCES

# Points at the charter's "The control plane tools" section; restates only the task-id rule.
_CONTROL_TOOLS_POINTER = (
    "Use the `mcp__plugin_jaah_control__*` tools as described in \"The control plane tools\" above. "
    "Never pass a task id (`task_id`, `task`, `taskId`); such a call is refused and writes nothing. "
    "`Control task id:` above your fence is for reference only. On `not_a_runner`, every task tool, "
    "`task_fail` included, is refused: say so in your final message and stop.\n"
)


# ── The settle section, rendered per persona ──
# Derived from the tables the settle endpoint validates against: `PERSONA_MANIFEST` / `PERSONA_TERMINALS`
# (`app/persona_registry.py`) and `TERMINAL_REFERENCES` (`app/task_settle.py`).

#: Runner-facing description of each terminal type. Key set must equal `TERMINAL_TYPES` (asserted in
#: `tests/test_dispatch_compose_pure.py`), else a settle step renders a blank ending.
_TERMINAL_PROSE: dict[str, str] = {
    "analysis": (
        "your settled analysis. It moves your stage to `done`. On a plan stage it declares there is NOTHING "
        "to build — the plan document is the epic's deliverable (your role section says when); refused (409) "
        "once any child has been appended"
    ),
    "coders": (
        "your decomposition. It moves your plan stage to `done`; refused (409) until at least one coder "
        "has been appended"
    ),
    "merged_pr": (
        "a PR you opened, watched, and SAW `jaah-forge pr view` report `state: merged`. It settles you "
        "`done`; nothing re-checks the merge"
    ),
    "no_pr": (
        "nothing merged: work you REPRODUCED as already done, or the no-PR ending your role section "
        "defines. It settles you `done`. Before it, close any PR you opened for this task: "
        "`jaah-forge pr close <n> --body-file <file>`, naming the reason"
    ),
    "finish": (
        "the epic's terminal phase. The server computes the finish report; your `summary` records what "
        "you verified"
    ),
    "outcome": "the work itself, delivered. It settles you `done`",
}

#: Description of each reference parameter; asserted to cover every name in `TERMINAL_REFERENCES`.
_REFERENCE_PROSE: dict[str, str] = {
    "pr_url": "the PR's URL; supplying it attests you watched this PR merge",
    "no_pr_reason": (
        "one of `duplicate`, `already_shipped`, `no_code_needed`, `remediated`; any other value is refused"
    ),
    "no_pr_evidence": (
        "the reproduction you RAN and what you OBSERVED (a commit message, grep hit or code presence is "
        "not evidence), or the evidence your role section defines for its own no-PR ending"
    ),
}


@dataclass(frozen=True)
class SettleTerms:
    """The three registry facts the settle section renders from, detached from the persona that keys them.

    `settle_terms_of(persona)` reads them off `persona_registry`; a caller holding the facts as data builds one
    directly and renders the same section through `_settle_tool_for` / `render_role_section_with`."""

    manifest: tuple[tuple[str, str], ...]
    terminals: tuple[str, ...]
    settle_states: tuple[str, ...]


def settle_terms_of(persona: str) -> SettleTerms:
    """One persona's `PERSONA_MANIFEST` / `PERSONA_TERMINALS` / `PERSONA_SETTLE_STATES` rows (absent → empty)."""
    return SettleTerms(
        manifest=tuple(PERSONA_MANIFEST.get(persona, ())),
        terminals=tuple(PERSONA_TERMINALS.get(persona, ())),
        settle_states=tuple(PERSONA_SETTLE_STATES.get(persona, ())),
    )


def folder_settle_terms(terms: SettleTerms) -> SettleTerms:
    """`terms` as they stand on a folder slot: a persona whose delivery is a PR (`merged_pr` among its
    terminals) has no repository, so it settles `outcome` instead and its report becomes HARD. Every other
    persona's terms are returned unchanged. The server's own swap (`task_settle.terminals_of` /
    `manifest_of`) is the authority; this mirrors it in the same order."""
    if "merged_pr" not in terms.terminals:
        return terms
    manifest = (("report", REQUIRED_HARD),) + tuple(row for row in terms.manifest if row[0] != "report")
    return SettleTerms(manifest=manifest, terminals=FOLDER_BUILD_TERMINALS, settle_states=terms.settle_states)


def _manifest_block_for(rows) -> str:
    """The artifact bullets for manifest `rows` (`(kind, level)` pairs), each with its obligation level.

    No rows renders the "you owe nothing" sentence: silence means no obligation."""
    if not rows:
        return (
            "   You owe no document artifact. The report below is still expected but is not a condition "
            "of settling.\n"
        )
    lines = []
    for kind, level in rows:
        extensions = ", ".join(f"`{ext}`" for ext in KIND_MEDIA[kind][0])
        obligation = (
            "**REQUIRED — settle is refused until it is uploaded**"
            if level == REQUIRED_HARD
            else "advisory — settle is accepted without it and the absence is recorded"
        )
        lines.append(
            f"   - `{kind}` ({ARTIFACT_KIND_LABELS[kind]}, {extensions}) — {obligation}.\n"
        )
    return "".join(lines)


def _terminal_block_for(terminals) -> str:
    """The `terminal_type` bullet(s) and their references for the endings in `terminals`."""
    allowed = terminals
    if not allowed:
        # No declared terminal: the CP refuses every settle, so tell the runner rather than render a call.
        return (
            "     Your role declares NO terminal type, so every settle is refused. Report this with "
            "`task_fail`.\n"
        )
    lines = []
    for terminal in allowed:
        lines.append(f"     - `terminal_type: \"{terminal}\"` — {_TERMINAL_PROSE[terminal]}.\n")
        for name in TERMINAL_REFERENCES[terminal]:
            lines.append(
                f"       - `{name}` — REQUIRED by this ending and accepted on no other: "
                f"{_REFERENCE_PROSE[name]}.\n"
            )
    if len(allowed) > 1:
        lines.append(
            "     Exactly ONE of those, with only its own references; another ending's reference is "
            "refused.\n"
        )
    return "".join(lines)


def _asking_clause_for(settle_states) -> str:
    """What asking costs at a settle door that accepts `settle_states`.

    Personas that settle from `awaiting_response` (reads the table, not the mode) are told not to settle
    with a round open; the rest are told their settle is refused while parked."""
    if AWAITING_RESPONSE in settle_states:
        return (
            "Asking parks you `awaiting_response` and settle is accepted from that state, but never settle "
            "with a round open: resolve the whole Q&A trail, act on it, then settle."
        )
    return (
        "Asking parks you `awaiting_response` and settle is refused from that state: finish the whole "
        "Q&A trail before you settle."
    )


def _settle_tool_for(terms: SettleTerms, *, summary_hint: str = "", gate_arg: str = "") -> str:
    """Render the terminal-settle section from `terms`.

    `summary_hint` and `gate_arg` are the per-role slots (summary content, gate verdict field); both may be
    empty."""
    hint = summary_hint or "your result and how it ended, in a line or two"
    return (
        "You finish with ONE call, `mcp__plugin_jaah_control__task_complete`. It "
        "checks that your role may claim the ending, that your task can settle from its state, and that everything you owe is "
        "uploaded. No comment, no label edit, no second step.\n\n"
        "1. **How you reach the control plane.** " + _CONTROL_TOOLS_POINTER + "\n"
        "2. **Upload what you owe FIRST:**\n\n"
        + _manifest_block_for(terms.manifest) +
        "\n"
        "   Upload each with `mcp__plugin_jaah_control__artifact_upload` — one call per kind, arguments "
        "`kind` and `path`. The extension must match the kind. Latest upload per kind wins.\n"
        "   **Write every file in `$JAAH_REPORT_DIR`, never in your <!--if:repo-->worktree<!--else:repo-->"
        "folder<!--end:repo-->;** a path outside it is refused.\n\n"
        "3. **Complete** by calling `mcp__plugin_jaah_control__task_complete` with these arguments:\n\n"
        f"   - `summary` (REQUIRED): <{hint}>. **Aim for under {OUTCOME_SUMMARY_TARGET_CHARS} "
        "characters.** Say what you did and how it ended. Anything you must NAME here (an assumption, a "
        "deferred follow-up, a blockage) gets a clause; its reasoning goes in your report<!--if:repo--> "
        "and PR body<!--end:repo-->. Length is not enforced.\n"
        "   - `terminal_type` (REQUIRED) — the ending you are claiming. Yours:\n"
        + _terminal_block_for(terms.terminals)
        + gate_arg +
        "\n"
        "4. **Read the response before you stop:** "
        "`{\"stored\":true,\"settled\":<bool>,\"state\":\"<state>\",...}`.\n"
        "   - `settled:true` (`state:\"done\"`) = success. Stop.\n"
        "   - A **422 naming missing artifact(s)**: upload each named kind with `artifact_upload` and "
        "call `task_complete` again. Your summary and references are kept.\n"
        "   - Any other refusal (409 on state, 422 on terminal type or references) recorded nothing. Fix "
        "what it names, or `task_fail` with its reason.\n"
        "   - On a tool ERROR, call `task_get` (no arguments). `null` means your task already settled: "
        "stop, do NOT `task_fail`. Otherwise retry ONCE, then `task_fail` naming the error.\n\n"
        "5. **Asking, blocked, failed.** <!--if:ask-->With Autonomous OFF, interview through the "
        "AskUserQuestion tool per the directive above the fence. "
        + _asking_clause_for(terms.settle_states) +
        " AskUserQuestion is your only channel to a human; you cannot park yourself and exit. Use it "
        "also for the security-surface gate and, in either mode, for a BLOCKER only a human can clear: a "
        "credential not in `credential_list`, an access you lack, a change in a system you cannot "
        "reach. Ask ONE round naming exactly what you need; if the answer does not clear it, deliver "
        "everything not blocked and settle with the remainder named. An irreducible failure (an "
        "unrecoverable error, a gate you cannot make green, a blocker the answer confirmed cannot be "
        "cleared)<!--else:ask-->You have no ask channel: an autonomous-OFF task runs as ON and you never "
        "settle from `awaiting_response`. For a BLOCKER only a human could clear, deliver everything not "
        "blocked, then name the blocker and who could clear it in your `task_fail`. An irreducible failure "
        "(a credential you lack, an unrecoverable error, a gate you cannot make green)<!--end:ask--> → "
        "`task_fail` with `reason`, and `session_id` set to the VALUE of the `CONTROL_RUNNER_SESSION_ID` "
        "environment variable (arguments are not shell-expanded; omit it if unset). Neither call is for "
        "a decision you can resolve and document<!--if:ask-->, and `task_fail` is not for a blocker you "
        "have not asked about<!--end:ask-->. Then stop."
    )
