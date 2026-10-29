"""The control plane's question when a node cannot clone or check out a task's repository.

A dispatch the node refuses identically `fleet_verdicts.REFUSAL_PARK_AFTER` times for a clone or checkout cause
(`is_checkout_refusal`) parks its task on one `task_questions` round raised here (`raise_round`) with three answers:
`RETRY`, `CHANGE_BRANCH` (the new branch typed as the answer's comment) and `CANCEL`. No runner exists to hand the
answer to, so the control plane enacts it: `answer_for` validates and applies a branch change before the round is
recorded, and `settle` moves the task once it is. The round is marked by `guard_context[awaiting.CP_GATE_KEY]`,
which keeps the reclaimers that end a runner's unanswered round off it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import actors, fleet_verdicts
from app.authz.catalog import permission_key
from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import has_project_key
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task, TaskQuestion
from app.models.worktrees import SlotIntent
from app.routes.tasks.awaiting import CP_GATE_KEY, await_response_on_wedged_dispatch
from app.task_lifecycle import AWAITING_RESPONSE, TRANSITION_BY_NAME

log = logging.getLogger(__name__)

GATE = "checkout"
HEADER = "Checkout failed"
RETRY = "Retry"
CHANGE_BRANCH = "Change integration branch"
CANCEL = "Cancel task"
OPTIONS = [
    {"label": RETRY, "description": "Fixed the cause (access, credential, branch); start again."},
    # `needs_comment`: the branch IS the comment (`answer_for`), so the console asks for it rather than sending it blank.
    {"label": CHANGE_BRANCH, "description": "Type the branch in the comment; the project switches to it, then "
                                            "starts again.", "needs_comment": True},
    {"label": CANCEL, "description": "Back to draft; nothing retries."},
]

# The node's clone and checkout refusals (`apps/jaahd/worktree_mechanics/`, `apps/jaahd/slot_repos.py`), after at
# most one of the lead-ins its enactors put on a `ResetRefused` (`apps/jaahd/reconcile/enact_launch.py`,
# `enact_reset.py`). Anchored on those exact phrases: a launch fault ("launch_runner: could not create …") must
# never read as a checkout.
_CHECKOUT_REFUSAL = re.compile(
    r"^(?:(?:claim mechanics|repository converge|reset) refused: )?"
    r"(?:(?P<clone>clone-failed|clone-unresolvable|clone-unusable|fetch-failed): "
    r"|integration branch '(?P<branch>[^']*)' does not exist"
    r"|(?P<checkout>git worktree[ ]add failed|could not (?:put \S+ on |detach \S+ onto origin/|land |create \S+: )))"
)
# Git's own words for a refused credential or an unreachable repository, anywhere in the detail.
_ACCESS_HINT = re.compile(r"could not read Username|Authentication failed|Permission denied|Repository not found|"
                          r"returned error: 40[134]", re.IGNORECASE)
# The shell's words when git runs a `credential.helper` that is not on disk — dash's "<helper> get: 1: <helper>: not
# found", bash's "<helper> get: line 1: <helper>: No such file or directory": the node's install is broken, not the
# repository or its credential.
_HELPER_MISSING = re.compile(r"\S{1,512} get: (?:line )?\d{1,6}: \S{1,512}: (?:not found|No such file or directory)")


def is_checkout_refusal(detail: Optional[str]) -> bool:
    return bool(_CHECKOUT_REFUSAL.match((detail or "").strip()))


def headline(detail: Optional[str]) -> str:
    """What failed and what to do, in one or two sentences; "" when `detail` is no clone or checkout refusal."""
    text = (detail or "").strip()
    found = _CHECKOUT_REFUSAL.match(text)
    if found is None:
        return ""
    if found.group("branch") is not None:
        return (f"Integration branch '{found.group('branch')}' does not exist in the repository and could not be "
                f"created from its default branch.")
    what = "clone the repository" if found.group("clone") else "check out the integration branch"
    if _HELPER_MISSING.search(text):
        return (f"The node could not {what}: its install is broken (git credential helper missing). "
                f"Re-run the node converge, then Retry.")
    hint = (" Check the project's repository URL and its Git access credential." if _ACCESS_HINT.search(text)
            else "")
    return f"The node could not {what}.{hint}"


def raise_round(session: Session, task_id: int, project_id: int, detail: str, refusals: int) -> bool:
    """Park task `task_id` on a checkout round: `ready|active → awaiting_response` and one open `task_questions`
    row. Commits. False when the task was not `ready`/`active` (settled, stopped, or already asking)."""
    # Paths, URL credentials and secret shapes (`redact_node_detail`): the round may reach Slack.
    said = fleet_verdicts.redact_node_detail(detail)
    question = (
        f"{headline(detail)} The task did not start; the node refused {refusals} times the same way.\n\n"
        f"The node said: {said}"
    )
    # Flushed first so the transition's own commit carries it: a parked task never lacks its round.
    session.add(TaskQuestion(
        task_id=task_id, question=question, options=OPTIONS, multi_select=False, header=HEADER, status="open",
        guard_context={CP_GATE_KEY: GATE, "project_id": project_id},
    ))
    session.flush()
    if await_response_on_wedged_dispatch(task_id, question, session) is None:
        session.rollback()
        return False
    return True


def _is_gate_round(row: TaskQuestion) -> bool:
    return (row.guard_context or {}).get(CP_GATE_KEY) == GATE


def answer_for(session: Session, task: Task, row: TaskQuestion, answer: str, comment: Optional[str],
               user: AdminUser, principal: Optional[Principal]) -> str:
    """`answer` unchanged; for `CHANGE_BRANCH` on a checkout round, first sets the project's integration branch
    to `comment` through the project editor's own write (`routes/projects.py::apply_project_update`, which
    validates the name, checks governance authority, audits and commits). 422 without a branch, 403 without
    `projects:edit` on the project; either leaves the round open. The project write commits before the round is
    recorded, so a later failure leaves the round open on an already-switched project; answering again rewrites
    the same value."""
    if not _is_gate_round(row) or answer.strip() != CHANGE_BRANCH:
        return answer
    from app.routes.projects import apply_project_update  # noqa: PLC0415 — a route module; only this answer needs it

    branch = (comment or "").strip()
    if not branch or len(branch.splitlines()) != 1:
        raise HTTPException(status_code=422, detail="Type the integration branch name in the comment.")
    project = session.scalars(
        select(Project).where(Project.id == task.project_id, Project.org_id == task.org_id)
    ).one_or_none() if task.project_id is not None else None
    if project is None or not project.has_repo:
        raise HTTPException(status_code=409, detail="This task's project has no repository to change.")
    acting = principal if principal is not None else principal_for_user(session, user)
    if not has_project_key(acting, permission_key("projects", "edit"), project.id):
        raise HTTPException(status_code=403, detail="Changing the integration branch needs projects:edit.")
    apply_project_update(session, project, {"integration_branch": branch}, user, acting)
    return answer


def settle(session: Session, task: Task, rows: list[TaskQuestion]) -> bool:
    """Enact a resolved checkout round: `CANCEL` resets the task to draft (and commits, then sends the slot
    intents the reset minted), `RETRY` and `CHANGE_BRANCH` are `answer`'s `awaiting_response → ready` for a fresh
    dispatch, and a dismissal leaves the task parked for Reset or Start. False when none of `rows` is a checkout
    round, so the caller hands them to a runner as usual."""
    row = next((r for r in rows if _is_gate_round(r)), None)
    if row is None:
        return False
    choice = (row.chosen_resolution or "").strip()
    if choice not in (RETRY, CHANGE_BRANCH, CANCEL):
        log.info("task %s: checkout round dismissed — left %s", task.id, task.state)
        return True
    if choice == CANCEL:
        # Route modules, imported here: they import this module's caller.
        from app.routes.tasks.slots import send_minted_intents  # noqa: PLC0415
        from app.routes.tasks.transitions import _reset_row  # noqa: PLC0415

        landed, issued = _reset_row(task.id, session)
        session.commit()
        send_minted_intents(issued)
        log.info("task %s: checkout round answered %r — reset to draft (%s)", task.id, CANCEL, landed)
        return True
    resume = TRANSITION_BY_NAME["answer"]
    moved = session.execute(
        update(Task)
        .where(Task.id == task.id, Task.state == AWAITING_RESPONSE)
        .values(
            state=resume.target,
            github_synced_at=datetime.now(timezone.utc),
            **({"dispatch_attempts": 0} if resume.resets_attempts else {}),
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if moved:
        _retire_parked_orders(session, task.id)
    emit_console_event(session, entity="task", id=task.id, action=resume.target if moved else task.state,
                       record_transition=bool(moved))
    log.info("task %s: checkout round answered %r — %s", task.id, row.chosen_resolution,
             f"back to {resume.target} for a fresh dispatch" if moved else f"left {task.state}")
    return True



def _retire_parked_orders(session: Session, task_id: int) -> None:
    """Mark the task's parked `lease_slot` orders `superseded`, so a Retry is a fresh dispatch.

    `lease_drain._fail_abandoned_dispatches` fails a ready slot-holder whose newest order is `parked` and older
    than `abandoned_dispatch_fail_minutes`; a round answered after that window would otherwise be failed on the
    next pass instead of re-dispatched. A re-mint, and its own refusals, start a new order."""
    session.execute(
        update(SlotIntent)
        .where(
            SlotIntent.verb == fleet_verdicts.VERB_LEASE_SLOT,
            SlotIntent.outcome == fleet_verdicts.OUTCOME_PARKED,
            SlotIntent.payload["task_id"].astext == str(task_id),
        )
        .values(outcome=fleet_verdicts.OUTCOME_SUPERSEDED)
        .execution_options(synchronize_session=False)
    )
