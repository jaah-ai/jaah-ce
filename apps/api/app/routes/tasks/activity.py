"""Task activity: the activity log, the questions list and run metrics."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import (
    actors, agent_sessions, hipaa, merge_lane_state, node_host_metrics, node_log_history, node_session_usage, request_caller,
    runner_terminations, task_source,
)
from app.account_fold import emails_for_slugs
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import granted_account_slugs, readable_account_slugs, visible_session_clause
from app.db import get_session
from app.entity_shape import SYSTEM_ACTOR_ID, optional_uuid
from app.launch_failures import START_ELSEWHERE_ACTION
from app.models.agent_sessions import AgentSession
from app.models.audit import ActivityEvent
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.tasks import Task, TaskQuestion
from app.task_artifacts import (
    ACTIVITY_EXCLUDED_NOTE_KINDS,
    activity_row_is_visible,
    is_hidden_note_kind,
    visible_artifact_clause,
)
from app.task_lifecycle import (
    ACTIVE, AWAITING_RESPONSE, DONE, DRAFT, PAUSED, READY, SYSTEM_FAILED, _RUNNER_END_STATES,
)
from app.task_lookup import get_task_or_404
from app.task_run_metrics import compute_run_metrics
from app.routes.tasks.qa_render import TaskQuestionOut, _task_questions_out

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ── Read route: unified activity log ────────────────────────────────────────────────────────────────
# The Activity Log tab's one feed — creation, edits, runner start/end, transitions, plan fan-out, gates,
# merges — from the durable `activity_events` store the dwell analytics and Q&A trail use. Internal
# bookkeeping notes are excluded.

# Every key here outlives its producer: the feed is append-only, so historical rows keep their labels.
# Never prune a key for having no writer (`comment`, `merged`, `stage_output`, `killed_as_idle` are such).
_ACTIVITY_NOTE_LABELS: dict[str, str] = {
    "answered": "Question answered",
    # Spelled out: the fallback "Ignored" reads as if the task was ignored. The durable proof a human declined.
    "ignored": "Question ignored",
    # The durable record that a settled decision was reopened (its `data` holds the old value).
    "answer_revised": "Answer revised",
    "question_raised": "Question raised",
    "comment": "Follow-up posted",
    # Writer-dead (the retired per-stage settle verb); kept because the rows remain and are still dual-read
    # (`_SETTLED_OUTPUT_NOTE_KINDS`).
    "stage_output": "Stage output captured",
    # Migration 0122 moved the stored rows from `decompose_fanout`, so one label suffices.
    "plan_fanout": "Plan fan-out",
    # Writer-dead (the deleted `/outcome` verb); merges now appear as `settle_coder_on_merge`'s `task:done`.
    # Kept for historical rows; do not add a writer to justify it.
    "merged": "PR merged",
    "runner_session": "Runner session attached",
    # The runner's terminal declaration from its settle call; registered so it names an action.
    "outcome": "Outcome reported",
    # `task_note:runner_terminated` is not registered: its label depends on `data.cause`, derived by
    # `_note_label` from `runner_terminations.label_for`. Nor is `task_note:lane_redispatch`: `data.cause`, `data.pr`.
    # `killed_as_idle` below has no writer any more (the drain re-dispatches instead); kept for historical rows.
    "killed_as_idle": "Runner killed as idle",
}



def _note_label(action: str, data: dict[str, Any], actor: Optional[str] = None) -> str:
    """Human-readable label for a `task_note:<action>` content row.

    A registered constant for every kind but `runner_terminated`, whose label depends on its `data`
    cause and is derived in `app/runner_terminations.py`, `lane_redispatch` (`merge_lane.redispatch_label`), and
    `agent_message`, headed by `data.agent`.
    `actor` is the resolved label (`identities.person_label`), never the frozen email. Other actions fall back to a spaced,
    capitalized verb, so new kinds need no registry update."""
    if action == runner_terminations.NOTE_ACTION:
        return runner_terminations.label_for(data.get("cause"), actor)
    if action == merge_lane_state.REDISPATCH_NOTE:
        return merge_lane_state.redispatch_label(data)
    if action == "agent_message":
        # An SDK agent's `notify` (`routes/agent_sdk.py::notify`), headed as its Slack post is; the text is the summary.
        agent = data.get("agent")
        return f"{agent if isinstance(agent, str) and agent else 'Agent'} posted"
    return _ACTIVITY_NOTE_LABELS.get(action, action.replace("_", " ").capitalize())


def _transition_label(action: str, state: Optional[str]) -> str:
    """Human-readable label for a `task:<action>` transition row. `active` reads as runner start; "Start fresh
    elsewhere" reads as itself whatever state it left; a row whose resulting state is terminal reads as runner end
    whatever the verb; anything else falls back to a spaced, capitalized verb."""
    if action == ACTIVE:
        return "Runner started"
    if action == START_ELSEWHERE_ACTION:
        return "Started fresh elsewhere"
    if state in _RUNNER_END_STATES:
        return f"Runner ended ({action})"
    return action.replace("_", " ").capitalize()


def _is_hidden_artifact_note(
    event: ActivityEvent, hidden_before: dict[int, Optional[datetime]]
) -> bool:
    """Whether this feed row is an ARTIFACT note its owning Task has soft-deleted — the Python arm of the
    watermark, used where a SQL test would need an unsafe cast (see `get_task_activity`).

    Hideable kinds are `app/task_artifacts.py::is_hidden_note_kind` (only the bodied `task_note:outcome`
    is an artifact). A row whose owner is not in the map is not hidden — fail open, so nothing is
    silently dropped."""
    if not is_hidden_note_kind(event.kind, event.data):
        return False
    return _row_predates_its_reset(event, hidden_before)


def _row_predates_its_reset(
    event: ActivityEvent, hidden_before: dict[int, Optional[datetime]]
) -> bool:
    """Whether this feed row belongs to an attempt its owning Task has RESET away: is it older than ITS
    OWN owning Task's watermark? Keyed per row off `data->>'id'`, since an epic's feed carries its
    children's rows. The whole of what the Activity Log tab hides (`get_task_activity`), deliberately
    kind-blind, and the timestamp half of `_is_hidden_artifact_note`, which is not.

    Not redundant with that one: `_is_hidden_artifact_note` is the artifact-content rule shared with the
    system audit log (`app/routes/audit.py`), which keeps the trail that an attempt happened. The task's
    own feed is what the operator asked to read pristine after a Reset, so no kind list applies.

    A row whose owner is not in the map is not hidden (fail open). The comparison is
    `app/task_artifacts.py::activity_row_is_visible`, the strictly-older rule's home; the tab is empty
    right after a Reset by design (`record_task_transition` stamps `created_at` from `now()`, before the
    watermark's later `statement_timestamp()`, so even the `task:reset` row predates its mark)."""
    owner = (event.data or {}).get("id")
    try:
        mark = hidden_before.get(int(owner))
    except (TypeError, ValueError):
        return False
    return not activity_row_is_visible(event.created_at, mark)


class ActivityLogEntryOut(BaseModel):
    """One row of the Activity feed — either a `transition` (a `task:%` row) or a `note` (a `task_note:%`
    content row). `duration_seconds` is set only on a runner-end transition with a preceding `active`;
    `data` carries the raw JSONB payload.

    `summary` is expected to be empty on newer transitions (it restated `kind` and `data`); a note's
    summary is real prose, and historical rows keep their text. Clients render `summary` only when
    non-empty."""

    category: str
    action: str
    label: str
    summary: str
    state: Optional[str] = None
    created_at: datetime
    duration_seconds: Optional[float] = None
    # The name of the person who caused this row (`_event_actor_id`); null for a machine row and for
    # transitions written before they recorded their actor.
    actor: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    # A row from an attempt a Reset abandoned (`_row_predates_its_reset`): kept on the task's own feed as history,
    # rendered apart from the current attempt's rows.
    earlier_attempt: bool = False
    # How many identical consecutive rows this one stands for (`_COLLAPSED_NOTE_ACTIONS`); `data.last_at` is the
    # latest one's time when above 1.
    repeat_count: int = 1


# Notes a machine re-writes unchanged on every pass: consecutive identical ones (same owner, action and prose)
# collapse to one row with a count, so a node refusing the same dispatch twenty times reads as one line.
_COLLAPSED_NOTE_ACTIONS = frozenset({"dispatch_refused"})


def _repeats_previous(entries: list[ActivityLogEntryOut], entry: ActivityLogEntryOut) -> bool:
    """Whether `entry` restates the feed's last row (`_COLLAPSED_NOTE_ACTIONS`); if so, counts it there."""
    last = entries[-1] if entries else None
    if (entry.action not in _COLLAPSED_NOTE_ACTIONS or last is None or last.category != "note"
            or last.action != entry.action or last.summary != entry.summary
            or last.earlier_attempt != entry.earlier_attempt or last.data.get("id") != entry.data.get("id")):
        return False
    last.repeat_count += 1
    last.data = {**last.data, "last_at": entry.created_at.isoformat()}
    return True


def _event_actor_id(event: ActivityEvent) -> Optional[UUID]:
    """The person behind one feed row: a transition carries them in `data.actor_id`
    (`user_activity.record_task_transition`), a note in `user_id`. None for a machine row or a malformed id."""
    if not event.kind.startswith("task:"):
        return event.user_id
    raw = (event.data or {}).get("actor_id")
    return optional_uuid(raw) if isinstance(raw, str) else None


def _resolved_actor(event: ActivityEvent, names: dict[UUID, str]) -> Optional[str]:
    actor_id = _event_actor_id(event)
    return names.get(actor_id) if actor_id is not None else None


def _name_machine_creators(
    session: Session, entries: list[ActivityLogEntryOut], *, name_foreign_orgs: bool = False
) -> None:
    """IN PLACE: a `created` row nobody pressed, on a task a machine filed (any `source` but `console`), names who
    filed it. A webhook delivery made with an API key reads "<key owner> via <key name>" (the key's name alone once
    its owner is gone); any other trigger's filing "<trigger name> via <source label>" ("Webhook", "GitHub", …) —
    the trigger's own row, not a child a runner filed under its epic (which inherits only its source); every other
    filer "<name> via <brand>", or the brand alone when the filing's chain names nobody. That human is
    the row's `created_by_id`: a runner's filing takes the executor of the task that filed it, a schedule's its
    owner, the nightly memory job the system actor (named nobody). A runner's filing also gains
    `data.filed_by_task_id`, the task that filed it. Read-time, so rows written before the fix read the same."""
    from app.routes.settings import app_title_for

    unnamed = [
        entry for entry in entries
        if entry.category == "transition" and entry.action == "created" and entry.actor is None
    ]
    ids = {int(raw) for entry in unnamed if str(raw := entry.data.get("id", "")).isdigit()}
    if not ids:
        return
    filed = {
        row.id: row for row in session.execute(
            select(Task.id, Task.org_id, Task.source, Task.created_by_id, Task.created_by_task_id,
                   Task.created_by_api_key_name, Task.created_by_automation_name, Task.created_by_trigger_name)
            .where(Task.id.in_(ids), Task.source != task_source.CONSOLE)
        ).all()
    }
    if not filed:
        return
    org_id = next(iter(filed.values())).org_id
    names = actors.actor_names(
        session, (row.created_by_id for row in filed.values() if row.created_by_id != SYSTEM_ACTOR_ID),
        name_acting=True, home_org=org_id if name_foreign_orgs else None,
    )
    brand = app_title_for(session, org_id)
    for entry in unnamed:
        row = filed.get(int(raw)) if str(raw := entry.data.get("id", "")).isdigit() else None
        if row is None:
            continue
        human = names.get(row.created_by_id) if row.created_by_id is not None else None
        if row.created_by_api_key_name:
            entry.actor = f"{human} via {row.created_by_api_key_name}" if human else row.created_by_api_key_name
        elif (trigger := row.created_by_automation_name or row.created_by_trigger_name) and row.source.startswith(
                task_source.TRIGGER_PREFIX):
            entry.actor = f"{trigger} via {task_source.source_label(row.source)}"
        else:
            entry.actor = f"{human} via {brand}" if human else brand
        if row.created_by_task_id is not None:
            entry.data = {**entry.data, "filed_by_task_id": row.created_by_task_id}


def _strip_ungranted_account_slugs(
    session: Session, principal: Principal | None, entries: list[ActivityLogEntryOut]
) -> None:
    """Drop `account_slug` from every entry naming an account `principal` holds no grant on. IN PLACE.

    A named function so the one pass over the finished feed is visible (see `get_task_activity` for why
    it is one pass). Uses the bare account grant (`granted_account_slugs`), not the roster set: a row
    names no node, so the roster's node conjunct would withhold for nothing."""
    allowed = granted_account_slugs(session, principal)
    if allowed.unrestricted:
        return
    for entry in entries:
        slug = entry.data.get("account_slug")
        if slug is not None and not allowed.contains(slug):
            entry.data = {k: v for k, v in entry.data.items() if k != "account_slug"}


@router.get("/{task_id}/activity", response_model=list[ActivityLogEntryOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def get_task_activity(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[ActivityLogEntryOut]:
    """The Activity Log tab's full event feed — every `task:%` transition PLUS every `task_note:%` content row
    for this task, merged chronologically. Read-only; 404 on unknown id. An epic's direct children's rows
    roll onto the same feed via one query scoped to `{task_id} ∪ children(task_id)`. Duration and session
    id are derived at read time, pairing `active`→terminal per owning task id; a still-open phase gets a
    running duration.

    A session id always travels with its account: `data.account_slug` is folded and cleared in lockstep,
    so no row pairs one attempt's session with another's account. Usually both land on the
    `runner_session` note (posted just before `task:active`); the fold arms cover the inverted order.
    `data.account_email` is resolved from `account_fold` at read time in one query (sensed per-node data).

    The slug is gated on the reader's account grants (`_strip_ungranted_account_slugs`), since
    project `tasks:view` is a wide audience: no grant, no slug. The email additionally requires
    `system:view`. (`GET /api/worktrees` remains fleet-wide at `worktrees:view`, a deferred gap.)"""
    get_task_or_404(task_id, session)
    # The earlier attempts come back flagged rather than vanish on Reset: "what happened before?" is this tab's
    # question. The Live Cast viewer keeps the pristine read.
    entries = task_activity_entries(session, principal, task_id, include_reset=True, name_foreign_orgs=True)
    # A node's journal can name the org's other projects and accounts, and project `tasks:view` is a wide audience:
    # the lines need `system:view` org-wide, as the account email below does.
    sees_the_org = principal is None or principal.has_org("system:view")
    if sees_the_org:
        # The tail's lines are raw terminal output: the pane-log viewer's own floor (`sessions:edit`, its route's
        # `@feature`) and its visibility too, so they reach exactly who could open the viewer.
        _add_node_errors(session, entries,
                         pane_reader=principal if principal is not None and principal.has("sessions:edit") else None)
    _explain_terminations(session, entries, sees_every_node=sees_the_org)
    _add_session_engines(session, entries)
    return entries


# How many of a dead run's last pane-log lines ride its termination row (`data.pane_log_lines`).
PANE_LOG_LINES = 5


def _add_node_errors(
    session: Session, entries: list[ActivityLogEntryOut], *, pane_reader: Optional[Principal] = None,
) -> None:
    """Put `data.node_errors` on each termination row whose cause the node may explain
    (`node_log_history.NODE_EXPLAINED_ENDS`: `vanished`, `crashed`, …): the node's own warning-or-worse journal
    lines just before the note, `node_log_history.end_lines` (window, redaction, the same-org rule). Here, not in
    `task_activity_entries`: the Live Cast viewer shares that feed and never shows a node's log. Read time, so
    every row already written gains them; three queries, only for a feed carrying such a row.

    The node is the note's own `node` (the uuid `runner_terminations.record` keeps), else the owning task's
    session that started last before the note; the window opens no earlier than that session's start. That
    session's pane-log tail, when one was captured (`app/pane_logs.py`), is named as `data.pane_log_session`, and for a
    `pane_reader` its last `PANE_LOG_LINES` lines ride as `data.pane_log_lines` — a run that died before its first
    event (a `launch.sh` that exited) says why on the row itself. Read through the viewer's own
    `visible_session_clause`; never for a HIPAA org's session, whose every pane read is recorded at the viewer
    (`routes/sessions.py::get_session_pane_log`), so its row keeps the link only."""
    wanted: list[tuple[ActivityLogEntryOut, int]] = []
    for entry in entries:
        if entry.category == "note" and entry.action == runner_terminations.NOTE_ACTION \
                and entry.data.get("cause") in node_log_history.NODE_EXPLAINED_ENDS:
            try:
                wanted.append((entry, int(entry.data.get("id"))))
            except (TypeError, ValueError):
                continue
    owners = {owner for _, owner in wanted}
    if not owners:
        return
    orgs = dict(session.execute(select(Task.id, Task.org_id).where(Task.id.in_(owners))).tuples().all())
    runs: dict[int, list[tuple[datetime, Optional[UUID], UUID, bool]]] = {}
    for owner, node_id, started_at, session_id, has_pane_log in session.execute(
        select(AgentSession.task_id, AgentSession.node_id, AgentSession.started_at, AgentSession.id,
               AgentSession.pane_log_tail.is_not(None))
        .where(AgentSession.task_id.in_(owners))
        .order_by(AgentSession.started_at)
    ).tuples():
        runs.setdefault(owner, []).append((started_at, node_id, session_id, has_pane_log))
    placed: list[tuple[ActivityLogEntryOut, int, Optional[UUID], Optional[datetime]]] = []
    tails: dict[UUID, list[ActivityLogEntryOut]] = {}
    for entry, owner in wanted:
        before = [run for run in runs.get(owner, []) if run[0] <= entry.created_at]
        start, node_id, session_id, has_pane_log = before[-1] if before else (None, None, None, False)
        if has_pane_log:
            entry.data = {**entry.data, "pane_log_session": str(session_id)}
            if pane_reader is not None and session_id is not None:
                tails.setdefault(session_id, []).append(entry)
        node_id = optional_uuid(entry.data.get("node")) or node_id
        if node_id is not None:
            placed.append((entry, owner, node_id, start))
    if tails:
        # Only the tails a row names, read once: the stored form is already cleaned and redacted.
        for session_id, tail, org_id in session.execute(
            select(AgentSession.id, AgentSession.pane_log_tail, AgentSession.org_id)
            .where(AgentSession.id.in_(tails), visible_session_clause(pane_reader))
        ).tuples():
            if hipaa.org_is_on(session, org_id):
                continue
            lines = [line for line in (tail or "").split("\n") if line.strip()][-PANE_LOG_LINES:]
            for entry in tails[session_id] if lines else []:
                entry.data = {**entry.data, "pane_log_lines": lines}
    if not placed:
        return
    nodes = {
        row.id: row for row in session.execute(
            select(Node.id, Node.org_id, Node.recent_errors).where(Node.id.in_({p[2] for p in placed}))
        ).all()
    }
    for entry, owner, node_id, start in placed:
        node = nodes.get(node_id)
        lines = [] if node is None else node_log_history.end_lines(
            node_org=node.org_id, recent_errors=node.recent_errors, owner_org=orgs.get(owner),
            start=start, end=entry.created_at,
        )
        if lines:
            entry.data = {**entry.data, "node_errors": lines}


# The transition states after which a runner's next `task:active` is a fresh start, not a repeat of one already
# shown (`task_activity_entries`' collapse). With a `runner_terminated` note, what ends a run on the feed.
_RUN_CLOSING_STATES = frozenset({READY, SYSTEM_FAILED, DONE, PAUSED, DRAFT})


# The causes whose remedy is elsewhere: the place the runner ran failed it, so another node may not.
_MOVABLE_END_CAUSES = frozenset({
    runner_terminations.CAUSE_VANISHED, runner_terminations.CAUSE_NODE_FAULT,
    runner_terminations.CAUSE_LAUNCH_UNANNOUNCED, runner_terminations.CAUSE_LAUNCH_FAILED,
    runner_terminations.CAUSE_STALLED,
})
_MOVE_NEXT_STEP = "Retry, or move the task to another node."


def _explain_terminations(
    session: Session, entries: list[ActivityLogEntryOut], *, sees_every_node: bool,
) -> None:
    """Make each termination row say where it happened and what to do: `data.node_name` (the note's `node`
    uuid, named at read time so a rename is a rename), `data.vm_id` (its `vm`), and `data.next_step` for a
    cause whose place failed the run (`_MOVABLE_END_CAUSES`) when a node or VM is known. IN PLACE; two queries,
    only for a feed carrying a named node.

    The name is shown for a node of the owning task's org, or when `sees_every_node` (the reader holds org-wide
    `system:view`, the `_add_node_errors` bar): a shared node is another org's machine, and project `tasks:view`
    is a wide audience.

    A "Start fresh elsewhere" transition (`START_ELSEWHERE_ACTION`, its `data.node` the node the task left) gets
    the same `node_name` by the same rule, in the same two queries; never a `next_step`, the move being made."""
    rows = [
        entry for entry in entries
        if (entry.category == "note" and entry.action == runner_terminations.NOTE_ACTION)
        or (entry.category == "transition" and entry.action == START_ELSEWHERE_ACTION)
    ]
    if not rows:
        return
    wanted: set[UUID] = set()
    for entry in rows:
        try:
            wanted.add(UUID(str(entry.data["node"])))
        except (KeyError, ValueError):
            continue
    nodes = {
        row.id: row for row in session.execute(
            select(Node.id, Node.name, Node.org_id).where(Node.id.in_(wanted))
        ).all()
    } if wanted else {}
    owners = {str(entry.data.get("id")) for entry in rows}
    orgs = {
        str(owner): org for owner, org in session.execute(
            select(Task.id, Task.org_id).where(Task.id.in_([int(o) for o in owners if o.isdigit()]))
        ).tuples()
    } if nodes else {}
    for entry in rows:
        extra: dict[str, Any] = {}
        node = nodes.get(optional_uuid(entry.data.get("node")))
        if node is not None and (sees_every_node or node.org_id == orgs.get(str(entry.data.get("id")))):
            extra["node_name"] = node.name
        if entry.category == "transition":
            if extra:
                entry.data = {**entry.data, **extra}
            continue
        if entry.data.get("vm"):
            extra["vm_id"] = entry.data["vm"]
        if entry.data.get("cause") in _MOVABLE_END_CAUSES and (entry.data.get("node") or entry.data.get("vm")):
            extra["next_step"] = _MOVE_NEXT_STEP
        if extra:
            entry.data = {**entry.data, **extra}


def _add_session_engines(session: Session, entries: list[ActivityLogEntryOut]) -> None:
    """Put `data.session_engine` on every row carrying a `session_id`: where its link opens
    (`agent_sessions.session_engine`). IN PLACE; one query.

    A session with no row (an older feed, a launch that never recorded one) reads `console`: a vendor-viewer run
    always has its row, so no other run is ever sent to the vendor's viewer."""
    ids = {sid for entry in entries if isinstance(sid := entry.data.get("session_id"), str) and sid}
    if not ids:
        return
    engines = {
        sid: agent_sessions.session_engine(kind, runtime, dispatched)
        for sid, kind, runtime, dispatched in session.execute(
            select(AgentSession.agent_session_id, AgentSession.agent_kind, AgentSession.runtime,
                   AgentSession.dispatched_runtime)
            .where(AgentSession.agent_session_id.in_(ids))
        ).tuples()
    }
    for entry in entries:
        sid = entry.data.get("session_id")
        if isinstance(sid, str) and sid:
            entry.data = {**entry.data, "session_engine": engines.get(sid, agent_sessions.SESSION_ENGINE_CONSOLE)}


def task_activity_entries(
    session: Session, principal: Principal | None, task_id: int, *, include_reset: bool = False,
    name_foreign_orgs: bool = False,
) -> list[ActivityLogEntryOut]:
    """`get_task_activity`'s feed for an existing task (its docstring carries the rules), shared with the Live Cast
    viewer (`app/routes/live_public.py`).

    `include_reset`: also return the rows a Reset hid, flagged `earlier_attempt`, so the task's own Activity tab
    keeps the attempts before it. Off by default: the Live Cast viewer reads the task pristine.

    `name_foreign_orgs`: an actor from outside the task's org reads "(operator)" or "(<org>)" (`actors.actor_names`'s
    `home_org`). The signed-in console's opt-in only: the anonymous Live Cast viewer must not learn who staff work for.

    One `Runner started` per run: a `task:active` with no closing transition (`_RUN_CLOSING_STATES`) or
    termination since the last one is a repeat — `Runner resumed` after `awaiting_response`, dropped after
    another `active`. Decided per owning task (`data.id`), on the rows' own `state`."""
    from app import task_notify

    child_ids = session.scalars(
        select(Task.id).where(Task.parent_task_id == task_id)
    ).all()
    scoped_ids = [str(task_id), *(str(cid) for cid in child_ids)]
    # The feed obeys the watermark for every row (operator decision): after a Reset the task reads pristine
    # until the next attempt writes, or, with `include_reset`, shows the earlier attempts flagged. See
    # `_row_predates_its_reset`. Nothing is deleted. Filtered in Python for `_verify_resolution_report`'s cast reason.
    hidden_before = dict(session.execute(
        select(Task.id, Task.artifacts_hidden_before)
        .where(Task.id.in_([task_id, *child_ids]))
    ).all())
    events = [
        (event, hidden)
        for event in session.scalars(
            select(ActivityEvent)
            .where(or_(ActivityEvent.kind.like("task:%"), ActivityEvent.kind.like("task_note:%")))
            .where(ActivityEvent.data["id"].astext.in_(scoped_ids))
            .where(ActivityEvent.kind.notin_(ACTIVITY_EXCLUDED_NOTE_KINDS))
            # A notification that reached no one because nothing is set up: written as a task note before
            # those moved to `task_notify.UNROUTED_KIND`, and the same on every task, so noise here.
            # `IS DISTINCT FROM`: a row with no `detail` must stay, and `<>` would drop it on NULL.
            .where(or_(
                ActivityEvent.kind != f"task_note:{task_notify.NOTE_ACTION}",
                ActivityEvent.data["detail"].astext.is_distinct_from(task_notify.NO_ROUTE),
            ))
            # Same second → insert order. `created_at` is the writing transaction's START (`now()`), so a
            # row written last by a long transaction can stamp earlier than rows other transactions wrote meanwhile;
            # the id is drawn at insert. The feed shows whole seconds, so within one the id is the order shown.
            .order_by(func.date_trunc("second", ActivityEvent.created_at), ActivityEvent.id)
        ).all()
        if not (hidden := _row_predates_its_reset(event, hidden_before)) or include_reset
    ]

    # Who acted, as a name: one batched lookup over every row carrying a person (a machine row carries none).
    event_actors = actors.actor_names(
        session, (_event_actor_id(event) for event, _ in events), name_acting=True,
        home_org=session.scalar(select(Task.org_id).where(Task.id == task_id)) if name_foreign_orgs else None,
    )
    entries: list[ActivityLogEntryOut] = []
    last_active_at: dict[str, datetime] = {}
    last_session_id: dict[str, Optional[str]] = {}
    # Mirrors `last_session_id` key for key, including every `.pop()`: the account belongs to the session,
    # so a longer-lived map would leak the previous runner's account onto the next cycle (guarded by the
    # `…_stale_account_…` activity test).
    last_account_slug: dict[str, Optional[str]] = {}
    open_active_entry: dict[str, ActivityLogEntryOut] = {}
    # Per owner: the last transition's state, and the run started with nothing closing it since, keyed to its
    # attempt (`earlier_attempt`): a Reset's watermark is itself a boundary, written transition or not.
    last_state: dict[str, Optional[str]] = {}
    open_run: dict[str, bool] = {}
    # Per owner: whether its last row was an earlier attempt's. Its first current row crosses the Reset, which
    # ends whatever that attempt left open: no session, account or duration carries over it.
    in_earlier: dict[str, bool] = {}

    def _close_phase(owner_id: str, entry: ActivityLogEntryOut) -> None:
        """End `owner_id`'s open runner phase on `entry`, the row that ended it: its duration since the start, and
        the phase's session (with its account) when the row names none of its own. Clears every per-phase
        tracker, so the next `active` starts clean and the read-time pass runs no clock on a finished run."""
        # Never negative: rows inside one second are ordered by id, not by their stamp (the query above).
        entry.duration_seconds = max(0.0, (entry.created_at - last_active_at[owner_id]).total_seconds())
        sid = last_session_id.get(owner_id)
        if sid is not None and "session_id" not in entry.data:
            slug = last_account_slug.get(owner_id)
            entry.data = {**entry.data, "session_id": sid, **({"account_slug": slug} if slug else {})}
        for tracker in (last_active_at, last_session_id, last_account_slug, open_active_entry):
            tracker.pop(owner_id, None)

    for event, hidden in events:
        # Who pressed it from where is Audit's to show (`audit:view`), never every task viewer's.
        data = {k: v for k, v in (event.data or {}).items() if k not in request_caller.CALLER_KEYS}
        owner_id = str(data.get("id"))
        if in_earlier.get(owner_id) and not hidden:
            for tracker in (last_active_at, last_session_id, last_account_slug, open_active_entry, last_state,
                            open_run):
                tracker.pop(owner_id, None)
        in_earlier[owner_id] = hidden
        if event.kind.startswith("task:"):
            action = event.kind.removeprefix("task:")
            state = data.get("state")
            label = _transition_label(action, state)
            previous = last_state.get(owner_id)
            last_state[owner_id] = state
            repeat = owner_id in open_run and open_run[owner_id] == hidden
            if action == ACTIVE:
                if repeat and previous == ACTIVE:
                    # A second claim of a run already shown starting (a relaunch, a restated edge): it adds a row
                    # and nothing else. The phase's start, session and duration stay on the first.
                    continue
                if repeat and previous == AWAITING_RESPONSE:
                    label = "Runner resumed"
                open_run[owner_id] = hidden
            elif state in _RUN_CLOSING_STATES:
                open_run.pop(owner_id, None)
            entry = ActivityLogEntryOut(
                category="transition",
                action=action,
                label=label,
                summary=event.summary,
                state=state,
                created_at=event.created_at,
                actor=_resolved_actor(event, event_actors),
                data=data,
                earlier_attempt=hidden,
            )
            if action == ACTIVE:
                last_active_at[owner_id] = event.created_at
                last_session_id.pop(owner_id, None)
                last_account_slug.pop(owner_id, None)
                open_active_entry[owner_id] = entry
            elif state in _RUN_CLOSING_STATES and owner_id in last_active_at:
                # Any closing state ends the phase, not only a terminal one: a retryable end lands `ready`
                # (usually after its termination note, which then closed the phase first and this is skipped).
                _close_phase(owner_id, entry)
        else:
            action = event.kind.removeprefix("task_note:")
            if action == runner_terminations.NOTE_ACTION:
                open_run.pop(owner_id, None)
            if action == "runner_session":
                sid = data.get("session_id")
                slug = data.get("account_slug")
                prev_sid = last_session_id.get(owner_id)
                last_session_id[owner_id] = sid
                # A restatement of the same session must not blank its account; a new session id replaces it (a relaunch).
                if slug or sid != prev_sid:
                    last_account_slug[owner_id] = slug
                start_entry = open_active_entry.get(owner_id)
                if start_entry is not None and sid is not None:
                    already = start_entry.data.get("session_id")
                    # Drop the note only when it adds nothing. Fold once: the start row keeps the session that started the
                    # phase, so a relaunch cannot erase it.
                    if already is None:
                        start_entry.data = {**start_entry.data, "session_id": sid,
                                            **({"account_slug": slug} if slug else {})}
                        continue
                    # A restatement of the id already shown.
                    if already == sid:
                        continue
                    # A different id within one phase is a relaunch: render it, so both sessions stay visible.
            actor = _resolved_actor(event, event_actors)
            entry = ActivityLogEntryOut(
                category="note",
                action=action,
                label=_note_label(action, data, actor),
                summary=event.summary,
                state=None,
                created_at=event.created_at,
                # A termination's label already names who stopped it (`runner_terminations.label_for`).
                actor=None if action == runner_terminations.NOTE_ACTION else actor,
                data=data,
                earlier_attempt=hidden,
            )
            if action == runner_terminations.NOTE_ACTION and owner_id in last_active_at:
                # The run stops HERE: a retryable end writes this note then `task:ready`, so waiting for an end
                # state would leave the phase open and the read-time pass below would run its clock on.
                _close_phase(owner_id, entry)
            if _repeats_previous(entries, entry):
                continue
        entries.append(entry)

    _name_machine_creators(session, entries, name_foreign_orgs=name_foreign_orgs)

    # AC6.5: a runner phase still open at read time (no closing row or termination note yet) gets a running
    # duration; an earlier attempt's never does, a Reset having ended it.
    now = datetime.now(timezone.utc)
    for owner_id, started_at in last_active_at.items():
        start_entry = open_active_entry.get(owner_id)
        if start_entry is not None and not start_entry.earlier_attempt:
            start_entry.duration_seconds = (now - started_at).total_seconds()

    # The slug strip, one pass over the finished feed: `account_slug` reaches `entry.data` by three paths,
    # including the raw `data=data` passthrough that grepping the literal misses. Guarded because most feeds
    # name no account and this is a query.
    if any("account_slug" in entry.data for entry in entries):
        _strip_ungranted_account_slugs(session, principal, entries)

    # The account's email, joined once for the whole feed; an unobserved slug carries no email.

    # The address needs `system:view` ORG-WIDE (a widening key: `has_org`) or the agent principal,
    # independent of the strip, and uses the roster answer. Both queries sit behind one guard.
    named = [slug for e in entries if isinstance(slug := e.data.get("account_slug"), str) and slug]
    emails: dict[str, str] = {}
    # The roster answer, read once: the email and the name below both ride it.
    roster = readable_account_slugs(session, principal) if named else None
    if roster is not None and (principal is None or principal.has_org("system:view")):
        emails = emails_for_slugs(session, [slug for slug in named if roster.contains(slug)])
    if emails:
        for entry in entries:
            email = emails.get(entry.data.get("account_slug"))
            if email:
                entry.data = {**entry.data, "account_email": email}
    # The account's name (`accounts.name`, the label every surface shows), on the roster answer like the
    # email (a label is the roster's to show) but without its `system:view` bar; a reader the roster does
    # not show the row keeps the bare slug.
    if roster is not None:
        # Its id too, so the name links to the account's page (`/accounts/{id}`).
        names = {
            slug: (name, account_id) for slug, name, account_id in session.execute(
                select(Account.slug, Account.name, Account.id).where(
                    Account.slug.in_([slug for slug in set(named) if roster.contains(slug)])
                )
            ).tuples().all()
        }
        for entry in entries:
            account_name, account_id = names.get(entry.data.get("account_slug"), (None, None))
            if account_name:
                entry.data = {**entry.data, "account_name": account_name, "account_id": str(account_id)}
    return entries


# ── task_questions: read route (cockpit-side, require_user) ─────────────────────────────────────────
# The projection is single-homed above `TaskOut`; this standalone route serves status-scoped reads. The
# same history rides on `TaskOut.questions`.
@router.get("/{task_id}/questions", response_model=list[TaskQuestionOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def list_task_questions(
    task_id: int,
    status: Optional[str] = Query(default="open"),
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[TaskQuestionOut]:
    """A Task's `task_questions` rows, oldest first — the runner's blocking questions and the multi-round
    Q&A history. Defaults to `status=open`; pass `?status=` (empty) for every status, or a value to
    scope. 404 on an unknown task. `round` is derived over the full visible trail before filtering, so
    the 3rd question reports `round=3` under `?status=open` (see `_load_task_questions_out`)."""
    task = get_task_or_404(task_id, session)
    all_rows = session.scalars(
        select(TaskQuestion)
        .where(
            TaskQuestion.task_id == task_id,
            visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
        )
        .order_by(TaskQuestion.created_at, TaskQuestion.id)
    ).all()
    projected = _task_questions_out(session, list(all_rows))
    if status:
        return [q for q in projected if q.status == status]
    return projected


# ── Run metrics: the four deterministic numbers ─────────────────────────────────────────────────────
# A separate endpoint, not a `_task_out` field: it reads a subtree, its transition trail and the price
# table, which per board row would be three queries. The arithmetic lives in `app/task_run_metrics.py`.
class TaskRunMetricsOut(BaseModel):
    """A task's run metrics — it and every descendant. Mirrors `apps/web/src/types.ts::TaskRunMetrics`.

    Every number carries a status, since "nothing spent" and "nothing captured" differ. Semantics live
    on the TS type; derivations in the module."""

    task_id: int
    scope_task_ids: list[int] = []
    started_at: Optional[str] = None
    # Which absence a null `started_at` is: 'not_started' has nothing to show; 'unavailable' ran, and its
    # tokens and dollars are valid. A closed `Literal`, as for `token_status`.
    clock_status: Literal["measured", "unavailable", "not_started"]
    ended_at: Optional[str] = None
    in_progress: bool = False
    total_seconds: Optional[float] = None
    agentic_seconds: Optional[float] = None
    tokens_in: int = 0
    tokens_out: int = 0
    # A closed vocabulary: the front end's only guard against a meaningless `0 / 0` is equality against
    # these spellings, so a fourth would slip through. Mirrors `types.ts`; produced only by `_token_status`.
    token_status: Literal["measured", "partial", "missing"]
    # A decimal string, never a float (money computed in `Decimal`). Null when `cost_status` is 'unavailable'.
    cost_usd: Optional[str] = None
    cost_status: Literal["measured", "partial", "unavailable"]
    unpriced_models: list[str] = []
    # What THIS task's last dispatch launched (not its subtree's); null when it was never dispatched.
    model: Optional["TaskModelRunOut"] = None
    # Over THIS task's own runner sessions (not its subtree's), from their lifetime peaks
    # (`node_session_usage.task_peaks`): the largest RSS and CPU (percent of one core), and the summed CPU time.
    # Null when no session reported, and on the public Live page, which shows no host figures.
    peak_rss_kb: Optional[int] = None
    peak_cpu_pct: Optional[float] = None
    cpu_seconds: Optional[float] = None


class TaskModelRunOut(BaseModel):
    """"High → gpt-5.6-sol": the tier dispatched (null for an exact override), the concrete `--model` the
    CLI was given (null = the CLI's own default), and the model id its latest session recorded (null until
    one is seen). `models/tasks.py::Task.dispatched_tier` and `models/agent_sessions.py::AgentSession.model` are the homes."""

    tier: Optional[str] = None
    dispatched: Optional[str] = None
    ran: Optional[str] = None


TaskRunMetricsOut.model_rebuild()


def _task_model_run(session: Session, task_id: int) -> Optional[TaskModelRunOut]:
    """The `model` block of `GET /{task_id}/run-metrics`, or None when the task was never dispatched."""
    row = session.execute(
        select(Task.dispatched_tier, Task.dispatched_model).where(Task.id == task_id)
    ).first()
    ran = session.scalar(
        select(AgentSession.model)
        .where(
            AgentSession.task_id == task_id,
            AgentSession.model.is_not(None),
            # Claude Code's placeholder for a turn no model produced; never a model that ran.
            AgentSession.model != "<synthetic>",
        )
        .order_by(AgentSession.started_at.desc())
        .limit(1)
    )
    if row is None or (row.dispatched_tier is None and row.dispatched_model is None and ran is None):
        return None
    return TaskModelRunOut(tier=row.dispatched_tier, dispatched=row.dispatched_model, ran=ran)


@router.get("/{task_id}/run-metrics", response_model=TaskRunMetricsOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def get_task_run_metrics(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TaskRunMetricsOut:
    """Total run time, agentic time, tokens in/out and token cost for this Task AND its whole subtree, from
    the transition trail and captured per-model usage. Read-only; 404 on an unknown id. Works for any
    Task — leaf, live epic, or never finished."""
    metrics = compute_run_metrics(task_id, session)
    if metrics is None:
        # Spelled here: the scope query already answered existence, so `get_task_or_404` would ask again.
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return TaskRunMetricsOut(
        **metrics, model=_task_model_run(session, task_id), **node_session_usage.task_peaks(session, task_id)
    )


# ── Session usage: each runner session's CPU/RAM over time (`GET /api/tasks/{id}/session-usage`) ────────────


class TaskSessionUsageNodeOut(BaseModel):
    """The node a session ran on; `name` is "" once the node is deleted (the peak outlives it)."""

    id: str
    name: str


class TaskSessionUsagePointOut(BaseModel):
    at: str
    cpu_pct: float
    rss_kb: int


class TaskSessionUsageSessionOut(BaseModel):
    """One session's chart over the window and its lifetime peaks (null when no peak row exists)."""

    node: TaskSessionUsageNodeOut
    session_id: str
    points: list[TaskSessionUsagePointOut]
    peak_rss_kb: Optional[int] = None
    peak_cpu_pct: Optional[float] = None
    cpu_seconds: Optional[float] = None


class TaskSessionUsageOut(BaseModel):
    """The Metrics tab's per-session chart. The window is the DB clock's (`node_host_metrics.window_bounds`) and
    `bucket_secs` the host chart's width for the range, as `GET /api/nodes/{id}/metrics` serves them."""

    window_start: str
    window_end: str
    bucket_secs: float
    sessions: list[TaskSessionUsageSessionOut]


@router.get("/{task_id}/session-usage", response_model=TaskSessionUsageOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def get_task_session_usage(
    task_id: int,
    # 24h default; the ceiling is the series' retention, derived so it moves with it.
    hours: int = Query(24, ge=1, le=int(node_host_metrics.RETENTION_DAYS * 24)),
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TaskSessionUsageOut:
    """This Task's own runner sessions (not its subtree's): each one's CPU/RAM per bucket over the last `hours`
    and its lifetime peaks, only rows written under the task's own org (`app/node_session_usage.py`). Read-only;
    404 on an unknown id. A task no session reported for answers an empty `sessions`."""
    task = get_task_or_404(task_id, session)
    window_start, window_end = node_host_metrics.window_bounds(session, hours=float(hours))
    found = node_session_usage.task_sessions(session, task_id, task.org_id, hours=float(hours))
    wanted = {entry.node_id for entry in found}
    names = dict(session.execute(select(Node.id, Node.name).where(Node.id.in_(wanted))).tuples().all()) if wanted else {}
    return TaskSessionUsageOut(
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        bucket_secs=node_host_metrics.bucket_secs_for(float(hours) * 60.0 * 60.0),
        sessions=[
            TaskSessionUsageSessionOut(
                node=TaskSessionUsageNodeOut(id=str(entry.node_id), name=names.get(entry.node_id, "")),
                session_id=entry.session_id,
                points=[
                    TaskSessionUsagePointOut(at=point.at.isoformat(), cpu_pct=point.cpu_pct, rss_kb=point.rss_kb)
                    for point in entry.points
                ],
                peak_rss_kb=entry.peak_rss_kb,
                peak_cpu_pct=entry.peak_cpu_pct,
                cpu_seconds=entry.cpu_seconds,
            )
            for entry in found
        ],
    )
