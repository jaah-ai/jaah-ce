"""Runner terminations — the one activity row that says a runner stopped, and why.

One note kind, `task_note:runner_terminated`, written wherever the control plane concludes a running
runner stopped; label-only (`data.cause`, rendered by `CAUSE_LABELS` at read time; unknown → bare
prefix). Also where undelivered questions are dropped. The causes superset `app/runner_causes.py::CAUSES`
(derived, so the two cannot drift) with the control-plane-side terminations no runner can report about itself.

Not a termination: the settled-hold reap, placement refusals, a runner's own settle, the cutover-orphan
sweep, node-side kill enactment, the compliance-group cascade, a dead-inference hold release, and the
cold answer resume. `record_runner_break`'s residual write (a break that relaunched nothing) does record.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import runner_causes
from app.models.identity import AdminUser
from app.models.tasks import TaskQuestion
from app.user_activity import record_task_note

#: The note key a runner's own reason rides in (`record(detail=...)`).
DETAIL_KEY = "detail"

#: The note key the launch's account rides in (`record(account=...)`): an account id, never a secret.
ACCOUNT_KEY = "account"

# The `task_note:` action suffix. One kind for every cause: the cause is a field, so a new cause needs
# no reader to learn a new key.
NOTE_ACTION = "runner_terminated"

# ── The cause vocabulary (SSOT) ───────────────────────────────────────────────────────────────────
CAUSES: tuple[str, ...] = (
    # Every runner-reported or CP-inferred break, with its remedy, is documented in `runner_causes.CAUSES`.
    *runner_causes.CAUSES,
    # ── Control-plane-side terminations, which no runner can report about itself ──
    # The runner's own `/fail`: it was alive, it could not proceed, and it said so.
    "reported_failure",
    # The operator pressed Stop on a task with a live runner.
    "operator_stop",
    # The operator force-started another task into this one's place (`routes/tasks/transitions.py::force_start_task`);
    # re-queued `ready` on its own worktree. Distinct from `operator_stop` so the label is truthful.
    "operator_preempt",
    # The operator pressed Reset (`routes/tasks/slots.py::abandon_task_slots`).
    "operator_reset",
    # The operator pressed Start on a task whose runner was still alive — reachable only from
    # `awaiting_response`, where Start orphans the frozen runner.
    "operator_restart",
    # The operator pressed Complete on a task a runner was still working — including an `advisor` chat;
    # no path may end a runner silently.
    "operator_complete",
    # A sibling failure cancelled this one. Nothing writes this any more; kept so existing rows render.
    "epic_failed",
    # The operator converted the task's persona, parking its live children.
    "persona_changed",
    # An advisor chat held a slot past the unattended bound with nobody on its console.
    "chat_unattended",
    # A runner's question went unanswered past the timeout and the attempt was ended
    # (`routes/tasks/awaiting.py::expire_timed_out_awaiting_response_tasks`). Any persona, not just chats.
    "question_unanswered",
    # A runner's question went unanswered past the timeout, so the runner was stopped but the row stays
    # `awaiting_response` and answerable (`routes/tasks/awaiting.py::stop_runner_on_unanswered_question`). The
    # task was not given up on.
    "question_runner_stopped",
    # A hidden outcome-criterion system Task closed by its producer because the condition cleared
    # (`routes/agent/task_reads.py::resolve_system_task`).
    "condition_cleared",
    # A launch the node acked whose runner died without claiming `ready → active` within the dispatch grace
    # (`fleet_decide.ACKED_REDISPATCH_GRACE_SECONDS`), so the drain
    # re-dispatched it (`lease_drain::_take_for_dispatch`). Written on every charge so a relaunch loop is
    # visible on the timeline.
    "launch_unannounced",
    # A merge-lane run reported that this node cannot run it at all (its gate fence will not start), via
    # `task.fail` with `node_fault` (`routes/agent/task_terminal.py::fail_task`). One is enough to leave the node out for the
    # project's lane tasks (`app/launch_failures.py`).
    "node_fault",
    # Not a termination: the attempt a failed launch charged was given back, because the same node had failed the
    # task's launch under another account (`app/launch_failures.py::cross_account_refund_due`). Label only.
    "launch_refunded",
    # The control plane ended a merge-lane base-check run because the integration branch moved past the commit
    # it gates (`merge_lane.end_stale_base_checks`): its verdict could only be stale.
    "superseded",
    # The tenant's Jaah-account balance reached its floor; the system paused the task (`app/jaah_routing/pause.py`).
    "balance",
    # The platform Bedrock account stopped working; the system paused the task until it recovers.
    "jaah_unavailable",
)

# ── The call sites' handles on that vocabulary ────────────────────────────────────────────────────
# A name per cause, so a typo is a `NameError` at import rather than a row rendering the bare prefix.
CAUSE_VANISHED = "vanished"
CAUSE_LAUNCH_FAILED = "launch_failed"
CAUSE_STALLED = "stalled"
# The causes the CP infers from a runner's absence: the vanish backoff (`routes/tasks/dispatch.py::_vanish_backoff_holds`),
# the node-fault refund count and the relaunch-storm finding read them as one.
RUNNER_LOST_CAUSES: frozenset[str] = frozenset({CAUSE_VANISHED, CAUSE_LAUNCH_FAILED, CAUSE_STALLED})
CAUSE_REPORTED_FAILURE = "reported_failure"
CAUSE_SUPERSEDED = "superseded"
CAUSE_OPERATOR_STOP = "operator_stop"
CAUSE_OPERATOR_PREEMPT = "operator_preempt"
CAUSE_OPERATOR_RESET = "operator_reset"
CAUSE_OPERATOR_COMPLETE = "operator_complete"
CAUSE_OPERATOR_RESTART = "operator_restart"
CAUSE_EPIC_FAILED = "epic_failed"
CAUSE_PERSONA_CHANGED = "persona_changed"
CAUSE_CHAT_UNATTENDED = "chat_unattended"
CAUSE_QUESTION_UNANSWERED = "question_unanswered"
CAUSE_QUESTION_RUNNER_STOPPED = "question_runner_stopped"
CAUSE_CONDITION_CLEARED = "condition_cleared"
CAUSE_LAUNCH_UNANNOUNCED = "launch_unannounced"
CAUSE_NODE_FAULT = "node_fault"
CAUSE_LAUNCH_REFUNDED = "launch_refunded"
CAUSE_BALANCE = "balance"
CAUSE_JAAH_UNAVAILABLE = "jaah_unavailable"

# ── The causes that mean "a session ended and this task's work did not" ───────────────────────────
# Read by `routes/tasks/task_wire.py::_task_prior_session_ended`: whether the next runner is starting or resuming.
# Members are every cause a re-dispatch can follow with the work still owed. Excluded:
# `operator_reset` (an abandoned attempt — "start clean"; its rows are hidden anyway), and
# `operator_complete`, `superseded`, `condition_cleared`, `epic_failed`, `persona_changed` (the task ended or changed).
RESTART_HINT_CAUSES: frozenset[str] = frozenset({
    # Every runner break (`runner_causes.CAUSES`): the session ended, the work did not.
    *runner_causes.CAUSES,
    CAUSE_REPORTED_FAILURE,
    CAUSE_OPERATOR_STOP,
    CAUSE_OPERATOR_PREEMPT,
    CAUSE_OPERATOR_RESTART,
    CAUSE_CHAT_UNATTENDED,
    CAUSE_QUESTION_UNANSWERED,
    CAUSE_QUESTION_RUNNER_STOPPED,
    CAUSE_LAUNCH_UNANNOUNCED,
    CAUSE_NODE_FAULT,
    CAUSE_BALANCE,
    CAUSE_JAAH_UNAVAILABLE,
})

# The prefix every label shares, so the feed reads uniformly whatever stopped the runner.
_LABEL_PREFIX = "Runner terminated"

# Cause → the half of the label after the dash, worded as what the operator should know or fix.
CAUSE_LABELS: dict[str, str] = {
    "usage_limit": "usage limit reached",
    "credit_exhausted": "credit exhausted",
    "auth_expired": "credentials expired",
    "model_refused": "model not available to the account",
    "client_unsupported": "client no longer supported",
    "timeout": "request timed out",
    "api_error": "API error",
    "crashed": "process crashed",
    "vanished": "session vanished",
    "launch_failed": "never seen running",
    "stalled": "went silent",
    "unknown": "cause unknown",
    "reported_failure": "reported it could not finish",
    "operator_stop": "stopped by operator",
    "operator_preempt": "stopped to make room for a higher-priority task",
    "operator_reset": "reset by operator",
    "operator_complete": "completed by operator",
    "operator_restart": "restarted by operator",
    "epic_failed": "cancelled, its epic failed",
    "persona_changed": "task persona changed",
    "chat_unattended": "chat left unattended",
    "question_unanswered": "its question went unanswered",
    "question_runner_stopped": "stopped while its question waits for an answer",
    "condition_cleared": "the condition it was raised for cleared",
    "launch_unannounced": "its launch never announced itself",
    "node_fault": "this node cannot run it",
    "launch_refunded": "attempt not charged, this node failed the launch under another account too",
    "superseded": "superseded, the branch moved on",
    "balance": "paused, balance below floor",
    "jaah_unavailable": "paused, Jaah account unavailable",
}


# Cause → the label naming the acting human, used when the row has a resolvable actor; otherwise the
# anonymous wording above.
_ACTOR_CAUSE_LABELS: dict[str, str] = {
    "operator_stop": "stopped by {actor}",
}


def label_for(cause: Any, actor: Optional[str] = None) -> str:
    """The Activity feed's label for one termination row. `actor` is the resolved human name, or None.

    Degrades to the bare prefix for an absent, non-string or unknown `cause` rather than raising."""
    if isinstance(cause, str) and actor and cause in _ACTOR_CAUSE_LABELS:
        return f"{_LABEL_PREFIX} — {_ACTOR_CAUSE_LABELS[cause].format(actor=actor)}"
    if isinstance(cause, str) and cause in CAUSE_LABELS:
        return f"{_LABEL_PREFIX} — {CAUSE_LABELS[cause]}"
    return _LABEL_PREFIX


def record(
    session: Session, task_id: int, cause: str, *, user: Optional[AdminUser] = None,
    node: Optional[str] = None, detail: Optional[str] = None, vm: Optional[str] = None,
    account: Optional[str] = None,
) -> None:
    """Write one `task_note:runner_terminated` row for `task_id`. Does not commit — the caller does.

    Runs in the caller's transaction so a rollback takes the note with the state change. The summary
    is empty by design (the label is derived from `cause`; the feed hides blank summaries). An unknown
    `cause` is stored rather than rejected so the write never fails its transition.

    Also drops undelivered questions (`drop_undelivered_questions`), except for causes whose own path
    retires them (`_QUESTIONS_RETIRED_ELSEWHERE`). `launch_unannounced` is charged only from `ready`,
    where no round can be open.

    `node` (a node uuid) is recorded where the runner ran, for the per-node launch count
    (`app/launch_failures.py`); absent when the caller does not know it. `detail` is the runner's own
    reason, already redacted and capped by the caller, kept where nothing else keeps it: a retryable end
    discards `reason` (`routes/tasks/runner_settle.py::end_attempt`). A runner's own `/fail` passes one
    (`routes/agent/task_terminal.py::fail_task`). `vm` (a microVM id) names the VM that was the runner, so a
    vanished MicroVM run says which VM to look at (`reclaim_active_task`). `account` (an account id) is the account
    the launch spent, for the cross-account refund (`launch_failures.cross_account_refund_due`)."""
    extra: dict[str, Any] = {"cause": cause}
    if node is not None:
        extra["node"] = node
    if vm is not None:
        extra["vm"] = vm
    if account:
        extra[ACCOUNT_KEY] = account
    if detail:
        extra[DETAIL_KEY] = detail
    record_task_note(session, task_id, NOTE_ACTION, "", extra, user=user)
    if cause not in _QUESTIONS_RETIRED_ELSEWHERE and cause not in _QUESTIONS_KEPT_ANSWERABLE:
        drop_undelivered_questions(session, task_id)


# Causes whose own path already retires the task's rounds. Only operator Reset: a soft delete that
# stamps `tasks.artifacts_hidden_before` and must keep the rows as history
# (`tests/test_tasks_routes_transitions.py::test_reset_hides_every_question_round_but_keeps_the_rows`).
_QUESTIONS_RETIRED_ELSEWHERE: frozenset[str] = frozenset({CAUSE_OPERATOR_RESET})

# Causes whose rounds must survive the runner: a runner stopped on an unanswered question leaves the row
# `awaiting_response`, and the answer goes to a fresh runner via its prompt's Q&A trail.
_QUESTIONS_KEPT_ANSWERABLE: frozenset[str] = frozenset({CAUSE_QUESTION_RUNNER_STOPPED})


def drop_undelivered_questions(session: Session, task_id: int) -> None:
    """Hard-delete every still-`open` `task_questions` round on `task_id`. Does not commit — same
    transaction as the termination row and the state change.

    An open round is answerable only by delivery into the live session that asked it; once that
    session is gone the round is a permanent phantom gate (the board's `?`). Delete, not `dismissed`:
    a dismissal records an operator declining to answer. The `task_note:question_raised` event keeps
    the history.

    Only `open` rows — answered/dismissed rounds are recorded resolutions. Not filtered by
    `artifacts_hidden_before`, so rounds nothing else can reach are cleared too. No activity row of its
    own."""
    session.execute(
        delete(TaskQuestion).where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "open",
        )
    )


def record_many(
    session: Session, task_ids: list[int], cause: str, *, user: Optional[AdminUser] = None,
) -> None:
    """`record` for a cascade (epic Stop, persona conversion). Same transaction, same non-commit
    contract, one row per task. The caller passes the rows that had a live runner before its guarded
    UPDATE — decided once, at the site that knows."""
    for task_id in task_ids:
        record(session, task_id, cause, user=user)
