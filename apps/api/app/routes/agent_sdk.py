"""The Jaah Agent SDK's control-plane surface: what an SDK agent's process reads, asks and posts about its own
task. The node daemon speaks these for it as the `agent.*` verbs (`apps/jaahd/agent_vocabulary.py`),
which carry no MCP tool, so a built-in runner's model never sees them (N9).

* `GET  .../agent-context`  — the task, its project's delivery rules, the agent itself, and who clears the
  security-surface gate (`dispatch_compose.directives.security_gate`: always `human` for an SDK agent — no review lane).
* `POST .../question-batch` — one to four questions as `task_questions` rounds, written by the SAME writer a
  runner's `AskUserQuestion` capture uses (`agent.record_question_round`), so they park the task, surface in
  Needs-attention and post Slack cards exactly like a runner's. Under Full Auto only a human-only blocker may
  be asked (`dispatch_compose.directives.FULL_AUTO_BLOCKER_HEADERS`), and at most four rounds are open at once.
* `GET  .../question-batch` — the rounds' resolutions, read back by the still-running agent: nothing is
  pasted into a pane (`tasks._deliver_answer_to_live_runner`'s SDK arm resumes the task instead).
* `POST .../notify` — an informational message to the task's channel (`task_notify.announce_agent_message`),
  capped per task per hour off its own alert rows, posted in-request under a task lock so the cap holds.

Auth is `routes/agent/auth.py::_check_token_for_live_task` (`@infra`; the token, bound to the declared caller task). Every
route refuses with a 409 a task the caller's node does not hold a live lease on (`agent._require_live_hold`)
and a task whose run is not an SDK agent's (`run_agent_id` unset), so a built-in runner's surface is
unchanged.
"""
from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import agent_runs, forges, node_credentials, secret_redact, slot_allocation, task_notify
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import SessionLocal, get_session
from app.dispatch_compose.directives import security_gate
from app.models.projects import Project
from app.models.tasks import Task, TaskQuestion
from app.routes.agent.task_terminal import _announce_question_round_in_task_scope, record_question_round
from app.routes.agent.auth import _check_token_for_live_task, _task_or_404_for_node
from app.routes.tasks.answers import _joined_selection_text, _NO_SELECTION_TEXT
from app.routes.tasks.qa_render import _question_options_out
from app.task_artifacts import visible_artifact_clause
from app.task_lifecycle import _RUNNER_RUNNING_STATES
from app.tenancy import seam
from app.user_activity import record_task_note

log = logging.getLogger("agent-sdk")

router = APIRouter(prefix="/api/agent", tags=["agent-sdk"])

# One `AskUserQuestion` call carries at most four questions; the SDK's `ask` keeps the same bound.
MAX_BATCH_QUESTIONS = 4
# The `ids` an answers read may name: a batch is four rounds, and an agent may read several batches at once.
MAX_ANSWER_IDS = 50

# The paragraph a task body opens with `Out of scope:` (the pipeline's R21), up to the next blank line or
# the `Done means:` block.
_OUT_OF_SCOPE_RE = re.compile(r"^Out of scope:[ \t]*(.*?)(?=\n\s*\n|\n\s*Done means:|\Z)", re.M | re.S)


def _sdk_task_or_409(session: Session, caller: node_credentials.AgentCaller, task_id: int) -> Task:
    """The caller's task, refused unless its run is an SDK agent's: these routes are that agent's surface, and a
    built-in runner reaches the operator through its own capture hook. The live hold is the route's dependency
    (`_check_token_for_live_task`)."""
    task = _task_or_404_for_node(session, caller, task_id)
    if task.run_agent_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is not run by an SDK agent; these routes serve only an SDK agent's run",
        )
    return task


# ── agent-context ─────────────────────────────────────────────────────────────────────────────────────
class AgentContextTask(BaseModel):
    id: int
    title: str
    body: str
    persona: str
    autonomous: bool
    # The body's `Out of scope:` paragraph, or None when it has none.
    out_of_scope: Optional[str]


class AgentContextProject(BaseModel):
    name: str
    # `owner/name` on the forge, or None when the project names no repo.
    repo: Optional[str]
    integration_branch: str
    branch_prefix: str
    gate_quick_cmd: str
    gate_full_cmd: str
    push_enabled: bool
    has_repo: bool


class AgentContextAgent(BaseModel):
    id: str
    display_name: str
    version: str
    commit: Optional[str]


class AgentContextOut(BaseModel):
    task: AgentContextTask
    project: AgentContextProject
    agent: AgentContextAgent
    # Who clears the security-surface gate on this run: `human` for every SDK agent (no review lane, AS1).
    security_gate: Literal["human", "agent"]


def out_of_scope(body: Optional[str]) -> Optional[str]:
    """The text of `body`'s `Out of scope:` paragraph, or None."""
    match = _OUT_OF_SCOPE_RE.search(body or "")
    text = match.group(1).strip() if match else ""
    return text or None


@router.get("/tasks/{task_id}/agent-context", response_model=AgentContextOut)
@infra(Tenancy.ORG_SCOPED)
def agent_context(
    task_id: int,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_live_task),
    session: Session = Depends(get_session),
) -> AgentContextOut:
    """What an SDK agent needs to do its task: the task, its project's delivery rules, itself, and the gate.
    409 for a task the caller's node does not hold, or one run by a built-in."""
    task = _sdk_task_or_409(session, caller, task_id)
    agent = agent_runs.run_agent(task.org_id, task.run_agent_id, task.run_agent_commit)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} names an agent its org cannot run")
    project = session.scalars(select(Project).where(Project.id == task.project_id)).one()
    repo = forges.repo_ref_or_none(project)
    return AgentContextOut(
        task=AgentContextTask(
            id=task.id, title=task.title, body=task.body or "", persona=task.persona,
            autonomous=bool(task.autonomous), out_of_scope=out_of_scope(task.body),
        ),
        project=AgentContextProject(
            name=project.name, repo=repo.path if repo is not None else None,
            integration_branch=project.integration_branch, branch_prefix=project.branch_prefix,
            gate_quick_cmd=project.gate_quick_cmd, gate_full_cmd=project.gate_full_cmd,
            push_enabled=project.push_enabled, has_repo=slot_allocation.task_has_repo(session, task),
        ),
        agent=AgentContextAgent(
            id=str(agent.id), display_name=agent.display_name, version=agent.version, commit=agent.commit,
        ),
        # No provider: an SDK run has no review lane, so this is `human` whatever the switch says.
        security_gate=security_gate(autonomous=bool(task.autonomous), provider=None),
    )


# ── question-batch: ask ───────────────────────────────────────────────────────────────────────────────
class BatchOptionIn(BaseModel):
    label: str = Field(min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=2000)


class BatchQuestionIn(BaseModel):
    # The bounds of `agent.TaskQuestionIn`, the runner's writer.
    question: str = Field(min_length=1, max_length=100_000)
    header: Optional[str] = Field(default=None, max_length=200)
    options: list[BatchOptionIn] = Field(default_factory=list, max_length=20)
    multi_select: StrictBool = False
    recommended_index: Optional[int] = Field(default=None, ge=0)


class QuestionBatchIn(BaseModel):
    questions: list[BatchQuestionIn] = Field(min_length=1, max_length=MAX_BATCH_QUESTIONS)


class QuestionBatchOut(BaseModel):
    ids: list[int]


def _stored_options(question: BatchQuestionIn) -> Optional[list[dict]]:
    """The options in the shape the capture hook stores (label/description objects, then a trailing
    `{"recommended_index": N}` marker), so every reader projects them alike (`tasks._question_option_details`)."""
    if not question.options:
        return None
    stored: list[dict] = [
        {"label": option.label, **({"description": option.description} if option.description else {})}
        for option in question.options
    ]
    if question.recommended_index is not None and question.recommended_index < len(question.options):
        stored.append({"recommended_index": question.recommended_index})
    return stored


@router.post("/tasks/{task_id}/question-batch", status_code=201, response_model=QuestionBatchOut)
@infra(Tenancy.ORG_SCOPED)
def ask_question_batch(
    task_id: int,
    body: QuestionBatchIn,
    background: BackgroundTasks,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_live_task),
    session: Session = Depends(get_session),
) -> QuestionBatchOut:
    """Raise one to four questions as open rounds. The first parks the task `awaiting_response`; each is
    announced to Slack like a runner's. 409 when no run is live or more than four rounds would be open; 422
    for a Full Auto question that is not a blocker (`agent.record_question_round`). The agent reads the
    answers back with `GET .../question-batch?ids=...`."""
    task = _sdk_task_or_409(session, caller, task_id)
    # Serialized per task, so two concurrent batches cannot both pass the open-round bound below.
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update())
    open_rounds = session.scalar(
        select(func.count()).select_from(TaskQuestion).where(
            TaskQuestion.task_id == task.id,
            TaskQuestion.status == "open",
            visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
        )
    ) or 0
    if open_rounds + len(body.questions) > MAX_BATCH_QUESTIONS:
        # An `ask` waits for its answers; a fresh batch on top of open rounds is not one, and it would turn
        # question cards into an uncapped Slack channel.
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} already has {open_rounds} open question(s); at most {MAX_BATCH_QUESTIONS} may "
                f"be open at once — wait for the answers first"
            ),
        )
    rows = [
        record_question_round(
            session, task, question=question.question, options=_stored_options(question),
            multi_select=question.multi_select, header=question.header,
        )
        for question in body.questions
    ]
    session.commit()
    ids = [row.id for row in rows]
    # After the commit (`create_task_question`'s reasons); never raises.
    from app.durable.question_plain_queue import enqueue_plain

    for question_id in ids:
        enqueue_plain(task_id, question_id)
    # Per round, off the request and after the commit — `create_task_question`'s reasons.
    for question_id in ids:
        background.add_task(_announce_question_round_in_task_scope, task_id, question_id, seam.current_scope())
    return QuestionBatchOut(ids=ids)


# ── question-batch: read the answers ─────────────────────────────────────────────────────────────────
class RoundOut(BaseModel):
    id: int
    status: Literal["open", "answered", "dismissed"]
    # The ticked or clicked option labels; [] when typed, dismissed, ticked none, or still open.
    selections: list[str]
    # The answer as recorded (`chosen_resolution`); "" while open and when dismissed.
    text: str
    # The operator's comment beside the answer, if any.
    comment: Optional[str]


class AnswersOut(BaseModel):
    rounds: list[RoundOut]
    # True once no named round is open.
    all_resolved: bool


# One round id: a positive ASCII integer inside int4 (`agent._CALLER_TASK_ID_RE`'s width), so `+1`, `-5`, `1_0`,
# `0`, a non-ASCII digit or an over-wide value is a 422 here rather than a lenient `int()` or a database 500.
_ROUND_ID_RE = re.compile(r"[1-9][0-9]{0,8}")


def _parse_ids(ids: str) -> list[int]:
    """`ids` as round ids: a comma-separated list of positive ints, nothing else — not empty, no blank item."""
    parts = ids.split(",")
    if not all(_ROUND_ID_RE.fullmatch(part) for part in parts):
        raise HTTPException(status_code=422, detail="ids must be a comma-separated list of positive round ids")
    parsed = [int(part) for part in parts]
    if len(parsed) > MAX_ANSWER_IDS:
        raise HTTPException(status_code=422, detail=f"name 1 to {MAX_ANSWER_IDS} round ids")
    return list(dict.fromkeys(parsed))


def split_selections(text: str, labels: list[str]) -> Optional[list[str]]:
    """The offered labels `text` joins (`tasks._joined_selection_text`, `", "`), in order, or None when it is
    not such a join (a typed answer). Matched label by label, so a label containing `", "` still parses."""
    # Selections are de-duplicated, so a join has at most one segment per label; more is a typed answer
    # (and would only deepen the walk).
    if not text or not labels or text.count(", ") >= len(labels) * max(label.count(", ") + 1 for label in labels):
        return None
    by_length = sorted(set(labels), key=len, reverse=True)

    def walk(at: int) -> Optional[list[str]]:
        for label in by_length:
            if not text.startswith(label, at):
                continue
            end = at + len(label)
            if end == len(text):
                return [label]
            if text.startswith(", ", end):
                rest = walk(end + 2)
                if rest is not None:
                    return [label, *rest]
        return None

    return walk(0)


def _round_out(row: TaskQuestion) -> RoundOut:
    if row.status == "open":
        return RoundOut(id=row.id, status="open", selections=[], text="", comment=None)
    if row.status == "dismissed":
        return RoundOut(id=row.id, status="dismissed", selections=[], text="", comment=None)
    text = row.chosen_resolution or ""
    labels = _question_options_out(row.options) or []
    if row.multi_select:
        selections = [] if text == _NO_SELECTION_TEXT else (split_selections(text, labels) or [])
        # A typed answer to a multi-select round is not a join of its labels: no selections, the text stands.
        if selections and _joined_selection_text(selections) != text:
            selections = []
    else:
        selections = [text] if text in labels else []
    return RoundOut(id=row.id, status="answered", selections=selections, text=text, comment=row.comment)


@router.get("/tasks/{task_id}/question-batch", response_model=AnswersOut)
@infra(Tenancy.ORG_SCOPED)
def read_question_batch(
    task_id: int,
    ids: str = Query(..., max_length=2000),
    caller: node_credentials.AgentCaller = Depends(_check_token_for_live_task),
    session: Session = Depends(get_session),
) -> AnswersOut:
    """The named rounds of this task, as they stand. Read-only: an id that is not a visible round of this task
    is a 404, never an invented round. `all_resolved` is false until every named round is answered or
    dismissed — the moment the agent's `ask` returns."""
    task = _sdk_task_or_409(session, caller, task_id)
    wanted = _parse_ids(ids)
    rows = {
        row.id: row
        for row in session.scalars(
            select(TaskQuestion).where(
                TaskQuestion.task_id == task.id,
                TaskQuestion.id.in_(wanted),
                visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
            )
        )
    }
    missing = [question_id for question_id in wanted if question_id not in rows]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"no round {', '.join(map(str, missing))} on task {task_id}"
        )
    rounds = [_round_out(rows[question_id]) for question_id in wanted]
    return AnswersOut(rounds=rounds, all_resolved=all(r.status != "open" for r in rounds))


# ── notify ────────────────────────────────────────────────────────────────────────────────────────────
class NotifyIn(BaseModel):
    message: str = Field(min_length=1, max_length=task_notify.AGENT_MESSAGE_MAX_CHARS)


class NotifyOut(BaseModel):
    # Handed to the task's channel; delivery is best-effort.
    queued: bool


@router.post("/tasks/{task_id}/notify", status_code=202, response_model=NotifyOut)
@infra(Tenancy.ORG_SCOPED)
def notify(
    task_id: int,
    body: NotifyIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_live_task),
    session: Session = Depends(get_session),
) -> NotifyOut:
    """Post an informational message to the task's channel (no answer expected). 429 past
    `AGENT_MESSAGES_PER_HOUR` in the last hour."""
    task = _sdk_task_or_409(session, caller, task_id)
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message is empty")
    if task.state not in _RUNNER_RUNNING_STATES:
        raise HTTPException(status_code=409, detail=f"task {task_id} is {task.state!r}; only a running agent may post")
    # Held until this request ends, so the next post counts this one's alert row: the post runs in-request.
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update())
    posted = task_notify.agent_messages_last_hour(session, task.id)
    if posted >= task_notify.AGENT_MESSAGES_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=(
                f"task {task_id} has posted {posted} messages in the last hour, the most allowed "
                f"({task_notify.AGENT_MESSAGES_PER_HOUR}); try again later"
            ),
        )
    # The task's own record of the post, under the agent's name (`routes/tasks/activity.py::_note_label`); redacted, as the
    # feed is read at `tasks:view` — and the same redacted text goes to Slack. Written whatever happens to the
    # Slack leg.
    message = secret_redact.redact(body.message)[0]
    agent = agent_runs.run_agent_name(task)
    record_task_note(session, task.id, "agent_message", message, {"agent": agent} if agent else {})
    _announce_message_in_task_scope(task.id, message)
    session.commit()
    return NotifyOut(queued=True)


def _announce_message_in_task_scope(task_id: int, message: str) -> None:
    """`task_notify.announce_agent_message` under the org of the task, on sessions of its own (`bind_from`, as
    `agent._announce_question_round_in_task_scope`). In-request, under the route's task lock, so the cap's
    count sees every earlier post. The route has already refused a caller that does not hold the task."""
    try:
        with SessionLocal() as session, seam.detached(), seam.bind_from(session, Task, task_id):
            # Release the pooled connection before the Slack relay; the scope is a ContextVar region.
            session.rollback()
            task_notify.announce_agent_message(task_id, message)
    except seam.TenancyRowMissing:
        log.warning("agent-sdk: task %s no longer exists — its agent's message was not posted", task_id)
