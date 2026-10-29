"""The break-cause vocabulary: why a runner session broke instead of finishing (SSOT).

A leaf on purpose — no `app.` imports — so both `app/runner_breaks.py` (the incident collection, which
re-exports it) and `app/runner_terminations.py` (whose termination causes superset it) read one tuple
without an import cycle through `app.routes.tasks`.
"""
from __future__ import annotations

# ── The cause vocabulary (SSOT) ─────────────────────────────────────────────────────────────────────
# The node-side mirror is `apps/jaahd/hooks/break_cause.py`'s `CAUSES`; the two move together. A
# cause earns a slot only if its remedy differs from every existing one.
CAUSES = (
    # The account's 5h/7d rolling window closed mid-run. Remedy: switch account, or wait for the reset.
    "usage_limit",
    # The org's spend cap / credit ran out. Remedy: top up; no account on that org escapes it.
    "credit_exhausted",
    # The account's OAuth credentials went invalid mid-session. Remedy: re-login that account+node.
    "auth_expired",
    # The provider will not serve the model to the account at all (Bedrock model access, AWS's "is not available
    # for this account"). Remedy: grant the account that model, provider-side; no retry clears it (`TERMINAL_CAUSES`).
    "model_refused",
    # The vendor no longer serves the CLI at all: "This client is no longer supported for Gemini Code Assist for
    # individuals". No re-login clears it, so it is no `auth_expired` and writes no account verdict (`TERMINAL_CAUSES`).
    "client_unsupported",
    # A request timed out. Remedy: usually a retry, the cheapest to recover from, so kept out of `api_error`.
    "timeout",
    # The API refused or failed: overload, 5xx, an internal error. Remedy: back off and retry.
    "api_error",
    # The process died with no readable transcript verdict. Remedy unknown; this bucket's size is the finding.
    "crashed",
    # CP-observed only, absent from the node mirror since no hook ran: inferred when the CP reclaims a task
    # whose runner is gone (`reclaim-active` / `reclaim-active-coder`).
    "vanished",
    # CP-observed only, like `vanished`: the runner was gone before its node ever sighted it running (acked, never
    # `last_seen_live`). Remedy differs: look at the launch on that node or account (startup crash, a walled
    # account, a node too starved to start it), not at the work. Split out of `vanished`.
    "launch_failed",
    # CP-observed only, like `vanished`: the task sat `active` past `runner_liveness.SILENCE_THRESHOLD` with no
    # session, node sighting or transcript write (`task_pipeline.reclaim_silent_active_tasks`). Remedy: look at
    # why the runner on that node or account never got going or hung, not at the work.
    "stalled",
    # No evidence and nothing matched. The floor, never a guess: a wrong cause distorts two buckets.
    "unknown",
)

# The causes no relaunch can clear: the attempt ends `system_failed` at once, its reason on the row
# (`routes/agent/runner_liveness.py::record_runner_break`), instead of spending the retry budget on the same refusal.
TERMINAL_CAUSES: frozenset[str] = frozenset({"model_refused", "client_unsupported"})
