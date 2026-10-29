"""Cockpit-task operator-channel notifications: blocked/failed alerts, the "shipped" and "assigned" cards.

A Task committing into `NOTIFY_STATES` records one `alerts` row via `app/alert_record.py`, in-process
(no `NODE_TOKEN`, no self-POST). `done` on a parentless task records a "shipped" alert with its own
one-shot claim (`done_stamp_key`), narrowed to the Task's own addressee. `announce_assignment` is
called by the task route after its commit. Driven by the post-commit `app.events` broadcaster.

This alert and `task_pipeline.sweep_needs_attention_notify` share one durable per-task claim, so one
message is sent; the sweep is the backstop. Dedup: an in-process `last_state` guard plus
`uq_alerts_dedup` (tumbling 300s window, `models.Alert`); no exactly-once. Best-effort throughout.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app import (
    agent_runs,
    alert_record,
    alert_relay,
    alert_stamps,
    edition_features,
    forges,
    notify_targets,
    org_names,
    task_pipeline,
    user_activity,
)
from app.operator_settings import reads as settings_reads
from app.db import SessionLocal
from app.durable import notify_outbox
from sqlalchemy import func, select

from app.models.audit import ActivityEvent
from app.models.notifications import Alert
from app.models.projects import Project
from app.models.tasks import Task, TaskQuestion
from app.node_identity import resolve_node_id
from app.task_artifacts import visible_artifact_clause
from app.task_lifecycle import AWAITING_RESPONSE, DONE, STATES, SYSTEM_FAILED, TRANSITION_BY_NAME

# The community edition has no Slack: `_settle_question_cards` returns before this name is read.
if edition_features.enabled("slack"):
    from app import slack_answer_echo

log = logging.getLogger("cockpit-task-notify")

# The only task actions that emit a needs-attention notification. `done` is announced by a separate
# leg and must never be added: membership decides who takes the per-task `needs-attention` claim (see
# `done_stamp_key`). These are task states; the alert `kind` is a separate vocabulary
# (`alert_relay.ALERT_KINDS`), translated in `_deliver_task_transition`.
NOTIFY_STATES = (AWAITING_RESPONSE, SYSTEM_FAILED)

# Separate from `needs-attention` (a task-id space) so a round id cannot collide with a task id.
# `task_questions.id` is a global PK, a valid bare scalar ident for `alert_stamps.key_for`.
QUESTION_ROUND_STAMP_FAMILY = "question-round"


def question_round_stamp_key(question_id: int) -> str:
    """The durable one-shot marker for one announced round: exactly one Slack card per round, ever.

    The round path's idempotency guarantee; the `alerts` window is only the coarser second line. Never
    re-armed (unlike `needs-attention`/`stuck-epic`): a round cannot recur, and re-arming would re-ask an
    answered question. Marks accumulate, bounded by questions ever asked."""
    return alert_stamps.key_for(QUESTION_ROUND_STAMP_FAMILY, question_id)


# Its own family: same family and ident as `needs-attention` would be the same `system_state` row.
DONE_STAMP_FAMILY = "task-done"


def done_stamp_key(task_id: int) -> str:
    """The durable one-shot marker for one announced shipment: one "shipped" card per task, ever.

    Not the `needs-attention` claim: that is keyed by task id too, and a task parked, announced, answered
    and then shipped would find it held and send no card. Never re-armed: `done` is terminal and a reopen
    is a human undoing it. Marks accumulate, bounded by top-level tasks ever shipped.

    Residual: `DbStampStore.claim` fails open, so an unreadable DB can duplicate a card; per-family
    posture was rejected to keep one store semantics."""
    return alert_stamps.key_for(DONE_STAMP_FAMILY, task_id)


def _notify_enabled() -> bool:
    """Master publish gate: the operator `notifications_enabled` override (Settings), else
    `JAAH_NOTIFY_ENABLED`, else on. Read at call time.

    `tests/conftest.py` pins it to `0` suite-wide; a misspelt name there would be a silently ignored
    variable on a gate preventing leaks to real phones. The producer half of three
    guards: the relay drops `test`-flagged events and `conftest.py` also pins `ALERT_RELAY_ENABLED=0`."""
    return settings_reads.get_bool("notifications_enabled")


def _emitting_node_id(skipped: str, ident: object) -> str | None:
    """The `NODE_ID` every alert here carries, or `None` when nothing is to be emitted: the gate above is off
    (`JAAH_NOTIFY_ENABLED=0`, the test-env signal — bail before any DB load or relay; the relay's `test`-flag
    drop backs this up) or `NODE_ID` is unset (this CP is not configured as a fleet member, so alerting stays
    off). Each is debug-logged, naming what `skipped` for `ident`, in the leg's own words."""
    if not _notify_enabled():
        log.debug("task-notify: emitting disabled — %s %s", skipped, ident)
        return None
    node_id = resolve_node_id()
    if not node_id:
        log.debug("task-notify: NODE_ID unset — %s %s", skipped, ident)
        return None
    return node_id


# Cap the reason text appended from a blocked-comment body so a large synced comment can't bloat the
# payload; the downstream relay still sanitizes untrusted URL-shaped substrings.
REASON_MAX = 500


def _notify_repo(project) -> str | None:
    """`owner/name` when the project's `repo_url` names an owner, else the bare name; None with no project.

    Deliberately `<owner>/<project.name>`, not the repo slug: an alert names the project an operator
    recognizes."""
    if project is None:
        return None
    repo = forges.repo_ref_or_none(project)
    if repo is not None:
        return f"{repo.path.split('/', 1)[0]}/{project.name}"
    return project.name


def _notify_reason(task, body: str | None = None) -> str:
    """The alert's human reason: the task title, plus the question body (truncated to `REASON_MAX`) when
    there is one.

    `body` defaults to `task.awaiting_response_body` (the transition leg). The round announcer passes the
    round's own text, since that column holds a batch's first question forever."""
    question = _question_body(task, body)
    return f"{task.title} — {question}" if question else task.title


def _question_body(task, body: str | None = None) -> str:
    """Just the question half of `_notify_reason`, without the Task's title.

    The title leads every rendered message, so the detail line carries the question alone;
    `_notify_reason` still composes `title — body` for the `alerts.reason` column. The default and
    truncation are spelled once here."""
    if body is None:
        body = task.awaiting_response_body
    if not body:
        return ""
    return body[:REASON_MAX] + "…" if len(body) > REASON_MAX else body


def build_task_notify_event(
    task, project, node_id, kind, reason_body: str | None = None, *, org: str | None = None,
    agent: str | None = None,
) -> dict:
    """Build the alert fields for a task transition, as `record_alert` keyword arguments.

    `kind` is passed by the caller, never re-read from `task.state`. `None` fields are dropped so
    `record_alert`'s defaults apply; `issue`/`url` are never sent (a console Task has no external issue).

    `payload={"task_id": task.id}` (spec `slack-two-way-communication` R5.1) makes a Slack reply
    resolvable to this Task. Security property: whoever sets `task_id` chooses which Task a reply
    answers, so the HTTP boundary strips it (`AlertIn`, R5.4a) and only this in-process producer sets it.

    `org` is the Task's org name (`org_names.org_name`), which leads the headline: a person in several orgs
    keeps getting every org's alerts, so each one says whose it is.

    `agent` is the display name of the SDK agent the task's run uses (`agent_runs.run_agent_name`), which
    every rendered message names; absent (no key at all) for a built-in runner, whose payload is unchanged."""
    event = {
        "kind": kind,
        "repo": _notify_repo(project),
        # Stays a name (env `NODE_ID`); `record_alert` resolves its own column (fail-open).
        "node_id": node_id,
        "reason": _notify_reason(task, reason_body),
        # Defense in depth: when publishing is gated off, self-mark so the relay drops it and
        # `record_alert` withdraws its dedup row. `None` when enabled, so the key is stripped. The second of
        # two gates (`_deliver_task_transition` bails first).
        "test": True if not _notify_enabled() else None,
        # R5.1: the Task id the correlation row is written from; never `None`, so the strip keeps it.
        # The other keys are the message text (`alert_relay` composes `{project}|{id} {title}: {status}`),
        # under the same trust argument (`_TRUSTED_ALERT_PAYLOAD_KEYS`): the HTTP boundary strips them all.
        # Spelled even when empty; `project` alone may be `None`.
        "payload": {
            "task_id": task.id,
            "project": project.name if project is not None else None,
            "org": org,
            "task_title": task.title,
            "question_body": _question_body(task, reason_body),
            **({"agent": agent} if agent else {}),
        },
    }
    return {k: v for k, v in event.items() if v is not None}


def _round_leg_owns_this_gate(session, task) -> bool:
    """Is this task's `awaiting_response` gate one the per-round announcer is announcing?

    True iff some open, visible `task_questions` round carries exactly `awaiting_response_body`; exact
    because `routes/tasks.await_response_on_dispatched_task` writes both from one string in one call.
    "Any open round" is not enough: a stale open round plus the roundless `/block` cold path would defer
    to a round leg that never runs. A Reset-hidden round is skipped by the announcer, so it cannot own
    the gate either."""
    body = task.awaiting_response_body or ""
    if not body:
        return False
    # The Task is in hand, so its watermark is read off the row, not joined.
    return session.scalars(
        select(TaskQuestion.id)
        .where(
            TaskQuestion.task_id == task.id,
            TaskQuestion.status == "open",
            TaskQuestion.question == body,
            visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
        )
        .limit(1)
    ).first() is not None


def _is_batch_follow_on(session, task, question_id: int) -> bool:
    """Was an earlier round of this Task announced and is it still open? Then this round belongs to the batch
    that one opened (a runner posts an `AskUserQuestion` batch one round at a time), and a brief recipient,
    already told by it, hears nothing of this one (`alert_relay.brief_skips`). Announced means its `alerts`
    row stands: a failed relay withdraws it, so a batch whose first post failed is still told by the next.
    Reset-hidden rounds are not outstanding."""
    return session.scalars(
        select(TaskQuestion.id)
        .where(
            TaskQuestion.task_id == task.id,
            TaskQuestion.id < question_id,
            TaskQuestion.status == "open",
            visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
            select(Alert.id).where(Alert.question_id == TaskQuestion.id).exists(),
        )
        .limit(1)
    ).first() is not None


def announce_question_round(task_id: int, question_id: int) -> None:
    """Announce one `task_questions` round to the operator channel.

    The unit of announcement is the round, not the state transition: rounds after the first update no
    row (`await_response_on_dispatched_task` is guarded on `ACTIVE`), so a transition-only watcher would
    show just the first round. `routes/agent/task_terminal.py::create_task_question` calls this per round it writes.

    Best-effort by contract: the caller is the runner's fail-open capture hook, so a Slack outage must
    never fail `AskUserQuestion`; every failure is logged and swallowed. Runs on the caller's worker
    thread and opens its own sessions, since `record_alert` commits."""
    # The whole body is inside the swallow: `create_task_question` calls this bare, and anything that
    # escapes 500s the route and fails the runner's `AskUserQuestion`.
    try:
        _announce_question_round(task_id, question_id)
    except Exception:
        log.exception(
            "task-notify: announcing question round %s for task %s failed — the question itself is "
            "recorded and the needs-attention backstop remains free to raise it",
            question_id, task_id,
        )


def _is_interactive_chat(task_id: int) -> bool:
    """Is this row a persona whose human is already at a console? One short-lived session, its own.

    The one home for the question in this module (two legs ask it; `routes/agent/` asks off the same
    map). Keyed on `DONE_CRITERION_OPERATOR`, never an `advisor` literal. False on an unreadable row, the
    safe answer: it announces."""
    # Deferred: `app.routes.tasks` pulls the routes layer into DB-free suites.
    from app.persona_registry import DONE_CRITERION_OPERATOR, PERSONA_DONE_CRITERION

    session = SessionLocal()
    try:
        task = session.get(Task, task_id)
        if task is None:
            return False
        return PERSONA_DONE_CRITERION.get(task.persona) == DONE_CRITERION_OPERATOR
    finally:
        session.close()


def _announce_question_round(task_id: int, question_id: int) -> None:
    """`announce_question_round`'s body, split out so the swallow wraps every statement."""
    node_id = _emitting_node_id("no card for question round", question_id)
    if node_id is None:
        return

    # A chat's round is answered in its own pane: the answerable card's path
    # (`_refuse_chat_answer_door_or_409`) 409s a chat, so the card could only offer a dead button.
    # Before the claims, like every early exit.
    if _is_interactive_chat(task_id):
        log.debug(
            "task-notify: task %s is an interactive chat — its round %s is answered in its own pane",
            task_id, question_id,
        )
        return

    stamps = alert_stamps.default_store()
    round_stamp = question_round_stamp_key(question_id)
    if not stamps.claim(round_stamp):
        log.info("task-notify: question round %s already announced — no second card", question_id)
        return

    # The per-task claim is taken too, and its result is not a gate. It tells the needs-attention
    # backstop this task's gate was announced. Round one wins it and later rounds lose it; only the round
    # claim gates, since the two answer different questions.
    attention_stamp = task_pipeline.needs_attention_stamp_key(task_id)
    stamps.claim(attention_stamp)

    def hand_the_claims_back() -> None:
        """Release everything needed for something to still reach the operator, for every outcome in which
        this call told them nothing. Whoever holds a claim must have spoken.

        The attention claim is released whether or not this call took it: round one holds it, so if a later
        round's relay fails the sweep would read "already announced" and lose that question. A duplicate flat
        line is the acceptable direction; a lost question is not. The round stamp release is semantic only
        (nothing re-runs a round today)."""
        stamps.release(round_stamp)
        stamps.release(attention_stamp)

    session = SessionLocal()
    try:
        task = session.get(Task, task_id)
        if task is None:
            # Deleted since the write; nothing announced, so the claims go back.
            log.info("task-notify: task %s is gone — no card for question round %s",
                     task_id, question_id)
            hand_the_claims_back()
            return
        # The card must not promise an answer the task cannot accept: the round is inserted
        # unconditionally, so a reclaimed task would get buttons `answer_task` refuses. Read from the
        # transition table so it tracks `answer.sources`.
        if task.state not in TRANSITION_BY_NAME["answer"].sources:
            log.info("task-notify: task %s is %r, which cannot accept an answer — no card for round %s",
                     task_id, task.state, question_id)
            hand_the_claims_back()
            return
        round_row = notify_targets.open_round(session, task_id, question_id)
        if round_row is None:
            # Answered, dismissed or gone since the route's commit: no card, but the claims still go back
            # (see `hand_the_claims_back`).
            log.info("task-notify: question round %s is no longer open — no card", question_id)
            hand_the_claims_back()
            return
        project = session.get(Project, task.project_id)
        # `blocked` is the alert kind a question translates to (as in `_deliver_task_transition`), and the
        # one `record_alert` requires for an answerable alert.
        built = build_task_notify_event(
            task, project, node_id, "blocked", reason_body=round_row.question,
            org=org_names.org_name(session, task.org_id), agent=agent_runs.run_agent_name(task),
        )
        if _is_batch_follow_on(session, task, question_id):
            built["payload"][alert_relay.BATCH_FOLLOW_ON_KEY] = True
    except Exception:
        # A DB fault before anything was sent: hand the claims back, then re-raise into the caller's
        # swallow, the one place that logs it.
        hand_the_claims_back()
        raise
    finally:
        session.close()

    try:
        outcome = _record_alert_in_new_session({**built, "question_id": question_id})
    except Exception:
        hand_the_claims_back()
        raise

    # Nothing reached the operator, so the claims go back. `deduped` included: an earlier alert owns
    # the window, so this card was never sent. A deliberate non-delivery is not released, or the round
    # re-announces forever.
    if outcome.relay_failed or outcome.deduped:
        hand_the_claims_back()


def announce_assignment(task_id: int, *, assignee_id, assigned_by_id) -> None:
    """Tell the person a Task was just assigned to that it is theirs: one brief personal card, to them
    alone.

    A leg of its own because an assignment changes only `tasks.assigned_to_id`, so no state event fires;
    `routes/tasks/` calls this after its commit, as `create_task_question` calls
    `announce_question_round`.

    Best-effort by contract: the whole body is swallowed so a notification problem never fails the edit.
    `assignee_id` is who the caller wrote; `assigned_by_id` the actor (`None` for a machine write). Both
    are re-checked against the row. Opens its own sessions, since `record_alert` commits."""
    try:
        _announce_assignment(task_id, assignee_id=assignee_id, assigned_by_id=assigned_by_id)
    except Exception:
        log.exception(
            "task-notify: announcing the assignment of task %s to %s failed — the assignment itself is "
            "committed and the console shows it",
            task_id, assignee_id,
        )


def _announce_assignment(task_id: int, *, assignee_id, assigned_by_id) -> None:
    """`announce_assignment`'s body, split out so the swallow wraps every statement.

    No stamp claim: assignments recur by design (A, then B, then A is three announcements). `record_alert`'s
    dedup identity carries no recipient, so two reassignments of one task inside one 300s bucket collapse
    into the first; accepted residual."""
    node_id = _emitting_node_id("no assignment card for task", task_id)
    if node_id is None:
        return
    if assignee_id is None or assignee_id == assigned_by_id:
        # A cleared assignment or a self-assignment announces nothing.
        log.debug("task-notify: task %s assignment names nobody else — no card", task_id)
        return

    session = SessionLocal()
    try:
        task = session.get(Task, task_id)
        if task is None:
            # Deleted since the write.
            log.info("task-notify: task %s is gone — no assignment card", task_id)
            return
        if task.hidden:
            # The shipped card's rule: a hidden row is maintenance nobody watches.
            log.debug("task-notify: task %s is hidden — no assignment card", task_id)
            return
        if task.assigned_to_id != assignee_id:
            # Superseded by a later edit, which sends its own card.
            log.info("task-notify: task %s is no longer assigned to %s — no card for a superseded "
                     "assignment", task_id, assignee_id)
            return
        project = session.get(Project, task.project_id)
        # Empty so a once-parked task does not carry its stale question on the reason line.
        built = build_task_notify_event(
            task, project, node_id, "assigned", reason_body="", org=org_names.org_name(session, task.org_id)
        )
    finally:
        session.close()

    # Only this in-process producer can set `assigned_by_id` (`AlertIn._strip_task_id` strips it at
    # the HTTP boundary), which lets `alert_record` trust "by <name>". Stringified for the JSONB column.
    if assigned_by_id is not None:
        built["payload"]["assigned_by_id"] = str(assigned_by_id)
    _record_alert_in_new_session(built)


# ── An SDK agent's own message to its task's channel ──────────────────────────────────────────────────
# The alert kind (`alert_relay.ALERT_KINDS`, CP-only) and the cap the `notify` route enforces, counted off the
# kind's own `alerts` rows: one hour, per task, with no table of its own.
AGENT_MESSAGE_KIND = "agent-message"
AGENT_MESSAGE_MAX_CHARS = 2000
AGENT_MESSAGES_PER_HOUR = 30


def agent_messages_last_hour(session, task_id: int) -> int:
    """How many messages task `task_id`'s agent has posted in the last hour: its `agent-message` alert rows.

    A withdrawn row (nothing reached anyone) is not counted, so a muted or failed post does not spend the cap."""
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    return session.scalar(
        select(func.count()).select_from(Alert).where(
            Alert.kind == AGENT_MESSAGE_KIND, Alert.task_id == task_id, Alert.created_at >= since,
        )
    ) or 0


def announce_agent_message(task_id: int, message: str) -> None:
    """Post an SDK agent's informational `message` to its task's channel: one `alerts` row (the console's
    record) relayed to the task's addressee like any task alert, headed by the agent's display name
    (`payload["actor"]`: "Acme Bot posted").

    Best-effort, like every leg here: a Slack outage never fails the agent's call, so every failure is
    logged and swallowed. Runs off the request (the route queues it), on sessions of its own."""
    try:
        _announce_agent_message(task_id, message)
    except Exception:
        log.exception("task-notify: posting an agent message for task %s failed — nothing was sent", task_id)


def _announce_agent_message(task_id: int, message: str) -> None:
    node_id = resolve_node_id()
    if not node_id:
        log.debug("task-notify: NODE_ID unset — no agent message for task %s", task_id)
        return
    session = SessionLocal()
    try:
        task = session.get(Task, task_id)
        if task is None:
            return
        built = build_task_notify_event(
            task, session.get(Project, task.project_id), node_id, AGENT_MESSAGE_KIND, reason_body=message,
            org=org_names.org_name(session, task.org_id),
        )
        # Tenant prose reaching an mrkdwn field: escaped so `<!channel>` cannot ping (`comment_notify`'s recipe).
        built["payload"]["question_body"] = alert_relay.inert_mrkdwn(message)
        built["payload"]["actor"] = agent_runs.run_agent_name(task) or "Agent"
    finally:
        session.close()
    # A fresh `issue` per message: `uq_alerts_dedup` would otherwise fold every post in a 300s window into one.
    _record_alert_in_new_session({**built, "issue": f"{AGENT_MESSAGE_KIND}-{uuid.uuid4().hex[:12]}"})


def _deliver_task_transition(event: dict) -> None:
    """Load the transitioned Task (+ its Project) and record one alert. `kind` comes from the
    triggering `event["action"]` (validated by the caller), never a re-read of `task.state`, which may
    have moved on. Off (debug-logged) when `NODE_ID` is unset."""
    node_id = _emitting_node_id("skipping notify for task", event.get("id"))
    if node_id is None:
        return

    # Translate the state into an alert kind; never pass it through. `alert_relay.ALERT_KINDS` is a
    # separate vocabulary, and `awaiting_response`/`system_failed` are kinds nothing subscribes to. `kind` is
    # also part of the `uq_alerts_dedup` key, and this in-process path bypasses `AlertIn`'s validator.
    kind = "failed" if event.get("action") == SYSTEM_FAILED else "blocked"

    def _load_and_build() -> dict | None:
        """The DB half, as a blocking call, kept off the event loop.

        Opening a session inline on the single uvicorn loop blocks it for the whole `pool_timeout` under a
        saturated pool, starving `GET /api/health`. `None` means "nothing to announce"."""
        # Deferred: `app.routes.tasks` pulls the routes layer into DB-free suites. The criterion map is a
        # persona fact single-homed in `routes/tasks/`.
        from app.persona_registry import DONE_CRITERION_OPERATOR, PERSONA_DONE_CRITERION

        session = SessionLocal()
        try:
            task = session.get(Task, event["id"])
            if task is None:
                return None
            # An interactive row's human is already at the console. A chat (`DONE_CRITERION_OPERATOR`, not an
            # `advisor` literal) parks `awaiting_response` at every turn, so an alert would be noise per turn.
            # Before the claim so nothing is burned; `routes/agent/task_reads.py::agent_list_needs_attention` excludes the
            # same personas, so no backstop waits on it. Narrowed to `awaiting_response`: a chat's `system_failed`
            # must still be announced, or a dead chat is announced nowhere. Keyed off the loaded `task.state`.
            if (
                PERSONA_DONE_CRITERION.get(task.persona) == DONE_CRITERION_OPERATOR
                and task.state == AWAITING_RESPONSE
            ):
                log.debug(
                    "task-notify: task %s is an interactive %r row — its operator is at the console, so "
                    "there is no alert to send", task.id, task.persona,
                )
                return None
            # The round leg owns a question, so this leg stands aside: `announce_question_round` posts an
            # answerable card per round, and a second card here would duplicate round one. No claim taken here;
            # whoever announces claims. Scoped to `blocked` (a `system_failed` alert is this leg's). Matched on the
            # question text, not "an open round exists": the roundless park (`task_pipeline_cp.block_task`) writes
            # no round, so a stale open round would otherwise leave the park announced by nobody.
            if kind == "blocked" and _round_leg_owns_this_gate(session, task):
                log.debug(
                    "task-notify: task %s is parked on a question round — the per-round announcer owns it",
                    task.id,
                )
                return None
            project = session.get(Project, task.project_id)
            built = build_task_notify_event(
                task, project, node_id, kind, org=org_names.org_name(session, task.org_id),
                agent=agent_runs.run_agent_name(task),
            )
        finally:
            session.close()
        return built

    built = _load_and_build()
    if built is None:
        return

    # One condition, one message. Both `NOTIFY_STATES` are also needs-attention states, so this leg and
    # `task_pipeline.sweep_needs_attention_notify` share one durable per-task claim; whoever takes it
    # speaks. This side normally wins (it fires off the commit; the sweep waits
    # `task_pipeline.NOTIFY_GRACE_SECONDS`), deliberately, as the richer message. Claimed before the alert
    # so a mid-relay sweep tick cannot send a second; released below if nothing was delivered.
    # Stamp calls each open a session and commit.
    stamps = alert_stamps.default_store()
    stamp = task_pipeline.needs_attention_stamp_key(event["id"])
    if not stamps.claim(stamp):
        log.info("task-notify: task %s entry already announced (needs-attention claim held) — no second "
                 "alert for kind=%s", event["id"], kind)
        _note_claim_held(event["id"], kind)
        return

    # A fresh session for the alert alone: `record_alert` commits, and sharing a session would flush
    # someone else's writes and entangle a failed alert with the transition it describes.
    try:
        outcome = _record_alert_in_new_session(built)
    except Exception:
        # The sink raised (a bug; `record_alert` swallows channel faults). Release the claim and let the
        # consumer loop log the traceback.
        stamps.release(stamp)
        raise
    # Nothing reached the operator, so the claim goes back for the sweep's backstop. Release on
    # `relay_failed` and on `deduped` (an earlier alert owns the window, so this one was never sent; keeping
    # the claim would make `sweep_needs_attention_notify` skip the row forever). A deliberate non-delivery
    # keeps the claim, or the row re-alerts every tick. `getattr` for test doubles: absent means delivered.
    if getattr(outcome, "relay_failed", False) or getattr(outcome, "deduped", False):
        stamps.release(stamp)


def _deliver_done_transition(event: dict) -> None:
    """Load a Task that just committed into `done` and, when it is top-level, record the one alert that
    becomes its creator's "shipped" card.

    Simpler than `_deliver_task_transition`: no reason body, no round leg, and no second producer (the
    needs-attention sweep does not announce `done`).

    Top-level only: every `coder` child of a fan-out reaches `done` itself, and announcing each would
    train the recipient to mute the channel; the epic's own `done` is announced once. Visible only: a
    hidden Runner-cron row is scheduled maintenance.

    Best-effort; `notify_targets.resolve_slack_target(..., done=True)` narrows the audience to the
    creator, and with none `alert_record` relays nothing rather than falling to the shared chat."""
    # Gated before any DB load so a synthetic `done` never DMs a real person.
    node_id = _emitting_node_id("no shipped card for task", event.get("id"))
    if node_id is None:
        return

    def _load_and_build() -> tuple[int, dict] | None:
        """The DB half, as a blocking call, kept off the event loop (same rule as the transition leg).

        `None` means "nothing to announce" (the four early exits here). The task id rides back because the
        claim key needs it and the ORM instance must not outlive the session."""
        session = SessionLocal()
        try:
            task = session.get(Task, event["id"])
            if task is None:
                # Deleted since the emitting commit; nothing claimed yet.
                return None
            if task.hidden:
                # A hidden row sends no shipped card (this leg only; blocked/failed alerts still announce hidden
                # rows, since maintenance breaking matters). Its producer is a Runner cron with `hidden` set
                # (`app/automation_fire.py`). Before the claim, so nothing is burned; the live value
                # decides (`models/tasks.py:Task.hidden`). Debug: a maintenance cron passes here every firing.
                log.debug("task-notify: task %s is hidden — no shipped card for scheduled maintenance",
                          task.id)
                return None
            if task.parent_task_id is not None:
                # A fan-out child; the common case on a busy epic, hence debug.
                log.debug("task-notify: task %s has a parent — its epic announces the shipment, not it",
                          task.id)
                return None
            # Still shipped? A reopen since the emitting commit would announce a non-shipment and burn the
            # one-shot claim, silencing the real one. Checked before the claim. Re-reading `task.state` is right
            # here: this leg's subject is a terminal state a human can undo.
            if task.state != DONE:
                log.info("task-notify: task %s is %s, not done, by the time the shipped card was built — "
                         "no card, and the claim stays unarmed for the real shipment", task.id, task.state)
                return None
            task_id = task.id
            project = session.get(Project, task.project_id)
            # Empty on purpose: `None` falls back to `task.awaiting_response_body`, which a park writes and an
            # answer never clears, so a once-blocked task would ship with a stale question.
            built = build_task_notify_event(
                task, project, node_id, "done", reason_body="", org=org_names.org_name(session, task.org_id),
                agent=agent_runs.run_agent_name(task),
            )
        finally:
            session.close()
        return task_id, built

    loaded = _load_and_build()
    if loaded is None:
        return
    task_id, built = loaded

    # One card per task, ever. Claimed before the alert so two events for one task cannot both send.
    # Its own family, never `needs-attention` (see `done_stamp_key`).
    stamps = alert_stamps.default_store()
    stamp = done_stamp_key(task_id)
    if not stamps.claim(stamp):
        log.info("task-notify: task %s has already been announced as shipped — no second card", task_id)
        _note_claim_held(task_id, "done")
        return

    # A fresh session for the alert alone; `record_alert` commits.
    try:
        outcome = _record_alert_in_new_session(built)
    except Exception:
        # The sink raised (a bug; `record_alert` swallows channel faults). Nothing was delivered, so release
        # the claim and let the consumer loop log the traceback.
        stamps.release(stamp)
        raise
    # Nothing reached anybody, so the claim goes back. Release on `relay_failed` (a retry can fix it),
    # `deduped` (an earlier alert owns the window, so this card was never sent) and
    # `recipient_misconfigured` (a fixable Slack recipient; this family is never re-armed). A deliberate
    # non-delivery (relay off, no channel, no human creator) keeps the claim, or every event re-attempts it.
    # `getattr` because a test double may omit fields: absent means delivered.
    if (
        getattr(outcome, "relay_failed", False)
        or getattr(outcome, "deduped", False)
        or getattr(outcome, "recipient_misconfigured", False)
    ):
        stamps.release(stamp)


def _note_claim_held(task_id, kind: str) -> None:
    """A second event found this entry's claim held: an earlier attempt sent it, or deliberately did not (a mute,
    no recipient), and either way owns it. Its `task_note:notification` row says so; the earlier row says which."""
    session = SessionLocal()
    try:
        _note_delivery(session, task_id, kind, "not_sent", "an earlier attempt handled it")
    finally:
        session.close()


def _record_alert_in_new_session(fields: dict) -> "alert_record.AlertOutcome":
    """Insert + relay one alert on its own session, record what became of it on the task's Activity
    (`_note_delivery`), and return its outcome.

    Blocking (a urllib POST inside `record_alert`), so the caller hops it off the event loop.
    Best-effort: `record_alert` swallows delivery faults and the teardown is guarded."""
    session = SessionLocal()
    try:
        outcome = alert_record.record_alert(session, **fields)
        # Returned so the caller can release its needs-attention claim on a delivery failure.
        if outcome.relay_failed:
            log.warning(
                "task-notify: alert for kind=%s repo=%s task=%s was NOT delivered to any operator "
                "channel (relay failed; the dedup row was withdrawn so a retry is not suppressed)",
                fields.get("kind"), fields.get("repo"), (fields.get("payload") or {}).get("task_id"),
            )
        if fields.get("kind") != AGENT_MESSAGE_KIND:
            # An agent's own post already has its row (`task_note:agent_message`).
            try:
                result, why = delivery_outcome(outcome, queued=not notify_outbox.in_workflow())
            except Exception:
                # A malformed outcome (a test double) must never fail the notifier it describes.
                log.exception("task-notify: unreadable outcome for kind=%s", fields.get("kind"))
            else:
                _note_delivery(session, (fields.get("payload") or {}).get("task_id"), fields.get("kind"), result, why)
        return outcome
    finally:
        session.close()


#: What a task notification is, as its Activity row names it (the alert kinds `task_notify` sends).
_NOTIFICATION_NAMES = {"blocked": "Question", "failed": "Failure", "done": "Shipped", "assigned": "Assigned"}
#: `task_note:<NOTE_ACTION>`: one per notification attempt, on the task's Activity and in Audit.
NOTE_ACTION = "notification"
#: Why a notification reached no one when nothing is set up to carry it. Said once in Settings
#: (`routes/settings.py::get_unrouted_notifications`), not on every task's Activity: the row is filed as
#: `UNROUTED_KIND`, outside the `task_note:` namespace the task feed reads.
NO_ROUTE = "no recipient or channel"
UNROUTED_KIND = "notification_unrouted"
_CHANNEL_NAMES = {"slack": "Slack", "slack_channel": "the Slack operator channel", "email": "email",
                  "telegram": "Telegram"}


def delivery_outcome(outcome: "alert_record.AlertOutcome", *, queued: bool = False) -> tuple[str, str]:
    """`(outcome, why)` for one attempt: `sent`, `queued` (handed to the outbox outside a workflow,
    `durable/notify_outbox.py`, whose worker logs a later refusal), or `not_sent` with the reason an operator
    debugging a missing message needs — never a provider's error text, which can carry a webhook URL."""
    if outcome.channels:
        return "queued" if queued else "sent", ", ".join(_CHANNEL_NAMES.get(c, c) for c in outcome.channels)
    if outcome.deduped:
        return "not_sent", "already sent in the last 5 minutes"
    if outcome.muted:
        return "not_sent", "the recipient muted it"
    if outcome.relay_failed:
        return "not_sent", "the provider refused it"
    if outcome.recipient_misconfigured:
        return "not_sent", "the recipient's Slack is unusable"
    if outcome.batch_follow_on:
        return "not_sent", "brief: the batch's first question was sent"
    return "not_sent", NO_ROUTE


def _note_delivery(session, task_id, kind, result: str, why: str) -> None:
    """The task's `task_note:notification` row for one attempt. Best-effort: a failed write is logged, never
    raised (the notification has already gone, or not)."""
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        return
    name = _NOTIFICATION_NAMES.get(kind or "", str(kind))
    summary = {"sent": f"{name}: sent by {why}", "queued": f"{name}: queued for {why}"}.get(
        result, f"{name}: not sent — {why}")
    try:
        if result == "not_sent" and why == NO_ROUTE:
            # The org's own setup gap, the same on every task: one Settings notice, not a row per task.
            session.add(ActivityEvent(
                action_type="write", kind=UNROUTED_KIND, summary=summary,
                data={"entity": "task", "id": task_id, "alert_kind": kind},
                # The task's org, as `record_task_note` files it (`activity_events.org_id` is NOT NULL).
                org_id=session.scalar(select(Task.org_id).where(Task.id == task_id)),
            ))
        else:
            user_activity.record_task_note(
                session, task_id, NOTE_ACTION, summary, {"alert_kind": kind, "outcome": result, "detail": why},
            )
        session.commit()
    except Exception:
        log.exception("task-notify: could not record the delivery outcome for task %s", task_id)
        with contextlib.suppress(Exception):
            session.rollback()


async def _run_task_notifier(broadcaster, stop: asyncio.Event) -> None:
    """Consume the cockpit-events broadcaster and enqueue one durable delivery per genuine task
    transition: into a `NOTIFY_STATES` state, into `done` for a top-level task, or out of
    `awaiting_response` with a Slack question card still open.

    Transport only: it watermarks, decides what is owed, and hands it to
    `app/durable/task_notify_queue.py`, so a deploy mid-relay recovers the delivery. One bad event never
    kills the loop.

    `last_state` suppresses a same-state re-sync (an unrelated edit re-emits `blocked`); any routine
    action resets the guard. It is in-process and lost on restart; `uq_alerts_dedup`'s 300s window
    backs it up (module docstring).

    Not leader-gated: every replica enqueues, and duplicates collapse on the deterministic workflow id,
    then the `alert_stamps` claim, then `uq_alerts_dedup`.
    """
    # Lazy: `app/durable/task_notify_queue.py` imports `dbos` and registers a workflow on import, which
    # this module's DB-free suite must not pull in.
    from app.durable.task_notify_queue import enqueue_notification

    queue = broadcaster.subscribe()
    last_state: dict = {}
    # A second watermark answering "was this task parked on a question a moment ago?". `last_state`
    # records every action (dependency edits included) and would answer "no" for a parked task. Same
    # restart caveat; neither dict evicts (one short entry per task id seen).
    last_task_state: dict = {}
    try:
        while not stop.is_set():
            event = await queue.get()
            try:
                if event.get("entity") != "task":
                    continue
                task_id = event.get("id")
                action = event.get("action")
                prev = last_state.get(task_id)
                last_state[task_id] = action  # record BEFORE the notify decision (routine actions reset the guard)
                # Advanced here in the transport: the settling workflow may run after a restart cleared this
                # in-process state, so the decision rides the payload.
                settle_cards = _is_question_card_departure(task_id, action, last_task_state)
                # `action != prev` on both arms so a re-sync never re-announces. `done` is not in `NOTIFY_STATES`.
                notifies = (action in NOTIFY_STATES or action == DONE) and action != prev
                if not settle_cards and not notifies:
                    continue
                # One enqueue carries both arms; `deliver_notification` fixes their order.
                payload = dict(event)
                payload["settle_cards"] = settle_cards
                # Flagged, not inferred: `action` may be a notify state whose `action != prev` guard already
                # failed, and the worker would otherwise re-alert.
                payload["settle_only"] = not notifies
                enqueue_notification(payload, alert_record.current_window_start())
            except Exception as exc:
                log.warning("task-notify: dropping event %s after error: %s", event, exc)
    finally:
        broadcaster.unsubscribe(queue)


def deliver_notification(event: dict) -> None:
    """The durable delivery for one event; `app/durable/task_notify_queue.py`'s workflow is the only caller.

    The card settle goes first because the alert leg re-raises on a DB or relay fault, and
    `system_failed` (both a notify state and a departure) is often a task's last event. The notify arms
    are mutually exclusive: `done` must never be added to `NOTIFY_STATES`.

    The watermark verdict (`settle_cards`, `settle_only`) is handed in by the transport, never
    re-derived: a workflow recovered after a restart has no watermark and would settle a card twice.
    """
    action = event.get("action")
    if event.get("settle_cards"):
        _settle_question_cards(event.get("id"), action)
    if event.get("settle_only"):
        # Settle only; the transport's watermark already suppressed the alert.
        return
    if action in NOTIFY_STATES:
        _deliver_task_transition(event)
    elif action == DONE:
        _deliver_done_transition(event)


def _is_question_card_departure(task_id, action, last_task_state: dict) -> bool:
    """Settle any Slack question card whose Task has just stopped being able to accept an answer.

    Here because no shared seam exists: `routes/tasks/` mirrors human resolutions, and
    `task_lifecycle.TRANSITIONS` has no executor.

    The test is `previous in (None, AWAITING_RESPONSE)`. `None` is the restart arm (the watermark is
    in-process); it is cheap on a task with no card, since `slack_answer_echo` checks `slack_threads`
    before calling Slack. Only real states are watermarked, so a non-lifecycle signal cannot hide a
    departure.

    True when this event is a departure worth settling cards for; the watermark is advanced either way."""
    if task_id is None or action not in STATES:
        # Defensive: every real emitter stamps an id.
        return False
    previous = last_task_state.get(task_id)
    last_task_state[task_id] = action
    return action != AWAITING_RESPONSE and previous in (None, AWAITING_RESPONSE)


def _settle_question_cards(task_id, action) -> None:
    """The Slack half, never raising. Called directly: inside a DBOS workflow it is already on a
    worker thread. `echo_resolution` swallows its own faults; the `except` is the belt."""
    if not edition_features.enabled("slack"):
        # No Slack in this edition, so no card to settle.
        return
    try:
        slack_answer_echo.echo_resolution(
            task_id=task_id,
            outcome=slack_answer_echo.OUTCOME_CLOSED,
            state=action,
        )
    except Exception as exc:
        log.warning(
            "task-notify: could not settle the Slack question card(s) for task %s on its move to %s (%s) "
            "— the transition itself stands", task_id, action, exc,
        )


async def start_task_notifier(app) -> None:
    """FastAPI startup hook (mirrors app/events.py's start_listener): subscribe the task-notify
    consumer to the cockpit-events broadcaster singleton and stash its handle on app.state."""
    from app.events import broadcaster

    stop = asyncio.Event()
    app.state.task_notifier_stop = stop
    app.state.task_notifier_task = asyncio.create_task(_run_task_notifier(broadcaster, stop))


async def stop_task_notifier(app) -> None:
    """FastAPI shutdown hook (mirrors app/events.py's stop_listener): signal the consumer to stop,
    cancel it, and await its clean exit."""
    from app.events import stop_background

    await stop_background(app, stop_attr="task_notifier_stop", task_attr="task_notifier_task")
