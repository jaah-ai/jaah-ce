"""The trusted autonomy directives: interview, Full Auto, and the security-surface gate. Their prose is the
edition's doctrine bundle's (`doctrine.BUNDLE.directives`); this module picks one."""
from __future__ import annotations

from app.agent_providers import AgentProvider
from app.dispatch_compose.doctrine import BUNDLE
# Re-exported: the settle route refuses a Full Auto question whose header is not one of these.
from app.dispatch_compose.role_tables import FULL_AUTO_BLOCKER_HEADERS  # noqa: F401


# The OFF directive for a CLI that cannot ask (`can_interview` False). Names the provider and states the
# recording duty (settle summary + report).
def _interview_unavailable_directive(provider: AgentProvider) -> str:
    """The OFF arm for a dispatch that cannot hold the promised interview because the agent has no question
    tool (`provider.can_interview`); not a reason to stop."""
    return (
        "Autonomous mode: OFF was recorded on this task, but the interview cannot be held, so run as if ON: "
        "ask nothing, and at every gate or decision point pick the recommended option and proceed.\n"
        f"{provider.label} has no question tool that reaches the operator.\n"
        "Resolve every ambiguity on the most defensible reading, record each as a documented decision "
        "(\"Assumed X because Y; say so if wrong\"), and state in your settle summary AND your report that "
        "THE INTERVIEW WAS SKIPPED because this provider cannot ask."
    )


def has_review_lane(provider: AgentProvider | None) -> bool:
    """Whether a run can clear the security-surface gate with the autonomous review lane: only on
    a charter-native provider's plugin roster (`charter_native`). `None` is an SDK agent's run, which never has
    one (AS1). The one predicate behind the `native` prompt flag and `security_gate`, so the two cannot drift."""
    return provider is not None and provider.charter_native


def security_gate(*, autonomous: bool, provider: AgentProvider | None) -> str:
    """Who clears the security-surface gate on a run: `agent` only under Full Auto WITH a review lane
    (`_FULL_AUTO_DIRECTIVE`'s `native` branch); otherwise `human` (its `else:native` branch, and OFF)."""
    return "agent" if autonomous and has_review_lane(provider) else "human"


def interview_skipped(
    *, autonomous: bool, console_present: bool, provider: AgentProvider,
) -> bool:
    """Was an interview promised on this dispatch and not held because the CLI cannot hold one?

    Single home for the question: `_autonomous_directive` renders off it and the console states it to the
    operator (`routes/tasks/`), so the two cannot drift. Terms, in `_autonomous_directive`'s order: a
    console role is never skipped; autonomous-ON promised nothing; then the absence of a question tool.

    Deliberately excludes `asks_at_gate` / `_NEVER_ASKS` (the prompt folds it in, but the operator
    sentence would be false for a stage that never asks); latent today since `planner` always has an epic
    parent."""
    return (
        not console_present
        and not autonomous
        and not provider.can_interview
    )


def _autonomous_directive(
    autonomous: bool, asks_at_gate: bool = True, console_present: bool = False,
    *, provider: AgentProvider,
) -> str:
    """R4 master-switch directive. Trusted: a fixed string keyed off a bool column and the role's ask
    licence, so no defanging. ON: `_FULL_AUTO_DIRECTIVE` (unattended — every decision the runner's, a question
    only for a human-only blocker). OFF:
    an interview (`_INTERVIEW_DIRECTIVE`) through AskUserQuestion, for every channel;
    the AskUserQuestion capture hook parks any task. `asks_at_gate` (`_NEVER_ASKS`) is the one role
    licence `delivery` cannot express.

    Order: `console_present` first (overrides both positions: an advisor flipped ON must keep talking);
    then `autonomous` (else an ON task on a non-asking CLI would be told "OFF was recorded"); then
    `provider.can_interview` False replaces both OFF arms with `_interview_unavailable_directive`."""
    if console_present:
        return f"Autonomous mode: {'ON' if autonomous else 'OFF'} — but {BUNDLE.directives['_CONSOLE_DIRECTIVE']}\n\n"
    if autonomous:
        return BUNDLE.directives["_FULL_AUTO_DIRECTIVE"]
    # The full shared predicate is passed, not just its undecided term, so this arm's condition is the
    # shared one.
    if interview_skipped(
        autonomous=autonomous, console_present=console_present, provider=provider,
    ):
        return f"{_interview_unavailable_directive(provider)}\n\n"
    if not asks_at_gate:
        return f"{BUNDLE.directives['_NO_CARVE_OUT_DIRECTIVE']}\n\n"
    # AskUserQuestion is the only ask channel; `_INTERVIEW_DIRECTIVE` says how it works.
    return f"{BUNDLE.directives['_INTERVIEW_DIRECTIVE']}\n\n"
