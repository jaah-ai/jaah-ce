"""The control plane's question when a node cannot clone or check out a task's repository.

A dispatch the node refuses identically `fleet_verdicts.REFUSAL_PARK_AFTER` times for a clone or checkout cause
(`is_checkout_refusal`) parks its task on one `task_questions` round raised here (`raise_round`) with three answers:
`RETRY`, `CHANGE_BRANCH` (the new branch typed as the answer's comment) and `CANCEL`. No runner exists to hand the
answer to, so the control plane enacts it: `answer_for` validates and applies a branch change before the round is
recorded, and `settle` moves the task once it is. The round is marked by `guard_context[awaiting.CP_GATE_KEY]`,
which keeps the reclaimers that end a runner's unanswered round off it.

A task's clone authenticates as the task's starter (`git_access.resolve_for_task`, served to the node through the
task arm of `/api/agent/tasks/{id}/git-token`); the (node, project) clone owner is only the no-task arm. So a clone
refusal is worded about the starter (`clone_refusal_sentence`, the one home of that wording, which `dispatch_wait`
serves too), and a `RETRY` of a clone round first asks the forge whether the starter's login can see the repository
at all (`retry_refusal`): a plain no keeps the round open with the reason, rather than re-dispatching into the same
refusal.

A clone refused because the starter's login cannot see the repository (`asks_starter`) parks on its FIRST refusal
(the node's `clone_refused` ack, `routes/agent/slot_intents.py`; retrying cannot change it) on a different round: it tells the starter they have no access and asks whether to start
anyway (`access_question`, answers `START_ANYWAY` / `DONT_START`). Yes frees the task's never-cloned git worktree and
puts it back in the queue to run without the repository (`tasks.run_without_repo`, a folder slot); No returns it to
draft.

A control-plane run (`ends_on_refusal`: the release executor, the merge lane, a compliance scan's auditor) is never asked
either round: it is hidden, so nobody would see the question, and a parked run keeps its project's releases, lane or scan
waiting on it (a parked auditor held its scan for the six-hour bound). Its refusal ends
it at once (`routes/agent/slot_intents.py`), and the next request files a fresh run.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import actors, fleet_verdicts, git_access, git_reach, slot_allocation
from app.forges import FORGE_LABELS
from app.merge_lane_state import LANE_PERSONA
from app.authz.catalog import permission_key
from app.authz.object_access import require_project_key
from app.authz.principal import Principal, principal_for_user
from app.clock import utcnow
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task, TaskQuestion
from app.models.worktrees import SlotIntent
from app.releases.executor import PERSONA as RELEASE_PERSONA
from app.routes.tasks.awaiting import CP_GATE_KEY, await_response_on_wedged_dispatch
from app.routes.tasks.policy import PERSONA_COMPLIANCE_AUDITOR
from app.task_lifecycle import AWAITING_RESPONSE, TRANSITION_BY_NAME

log = logging.getLogger(__name__)

# `_cloned_elsewhere`: how many of the person's newest tasks of the project are looked through for a clone on
# another node.
_ELSEWHERE_TASKS = 25

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

# The no-access round (`access_question`): one question, two answers, no header.
START_ANYWAY = "Yes"
DONT_START = "No"
ACCESS_OPTIONS = [
    {"label": START_ANYWAY, "description": "Runs without the repository."},
    {"label": DONT_START, "description": "Back to draft."},
]

# The node's clone and checkout refusals (`apps/jaahd/worktree_mechanics/`, `apps/jaahd/slot_repos.py`), after at
# most one of the lead-ins its enactors put on a `ResetRefused` (`apps/jaahd/reconcile/enact_launch.py`,
# `enact_reset.py`); a forge failure is `slot_clone.CloneFailed`'s "clone failed: <host>: <cause words>", the older
# hyphen forms kept for an older node. Anchored on those exact phrases: a launch fault ("launch_runner: could not
# create …") must never read as a checkout.
_CHECKOUT_REFUSAL = re.compile(
    r"^(?:(?:claim mechanics|repository converge|reset) refused: )?"
    r"(?:(?P<clone>clone-failed|clone-unresolvable|clone-unusable|fetch-failed|clone failed: [^:\n]{1,253}): "
    r"|integration branch '(?P<branch>[^']*)' does not exist"
    r"|(?P<checkout>git worktree[ ]add failed|could not (?:put \S+ on |detach \S+ onto origin/|land |create \S+: )))"
)
# Git's own words for a refused credential or an unreachable repository, anywhere in the detail.
_ACCESS_HINT = re.compile(r"could not read Username|Authentication failed|Permission denied|Repository not found|"
                          r"returned error: 40[134]", re.IGNORECASE)
# `slot_clone._CAUSE_WORDS` -> the cause it words, read off a "clone failed: <host>: <words>" refusal.
_NODE_CAUSES = {
    "authentication was refused": "auth",
    "the repository was not found": "access",
    "the forge could not be reached": "network",
    "the forge did not answer in time": "timeout",
    "the node's git credential helper is missing": "node_broken",
}
# The shell's words when git runs a `credential.helper` that is not on disk — dash's "<helper> get: 1: <helper>: not
# found", bash's "<helper> get: line 1: <helper>: No such file or directory": the node's install is broken, not the
# repository or its credential.
# `slot_clone`'s refusals before git runs (no URL, a URL carrying a credential, a parent dir it cannot create): the node's
# own words, never the forge's — a "Permission denied" there is the node's filesystem, not the repository's access.
_LOCAL_CLONE_STEP = re.compile(r"\bclone-unresolvable: ")
_HELPER_MISSING = re.compile(r"\S{1,512} get: (?:line )?\d{1,6}: \S{1,512}: (?:not found|No such file or directory)")


#: The control-plane runs a refusal ends rather than parks (`ends_on_refusal`).
_ENDS_ON_REFUSAL = frozenset({RELEASE_PERSONA, LANE_PERSONA, PERSONA_COMPLIANCE_AUDITOR})


def ends_on_refusal(task: Optional[Task]) -> bool:
    """Whether a clone or checkout refusal ends `task` at once, with no round: a control-plane run nobody watches."""
    return task is not None and task.persona in _ENDS_ON_REFUSAL


def is_checkout_refusal(detail: Optional[str]) -> bool:
    return bool(_CHECKOUT_REFUSAL.match((detail or "").strip()))


def _clone_cause(text: str) -> str:
    """A clone refusal's cause: `node_broken` (the node's credential helper is missing), `auth` (the login was
    refused), `access` (the login cannot see the repository), `network`,
    `timeout`, or `other` (a local clone step, or an older node's words that name none of these)."""
    if _HELPER_MISSING.search(text):
        return "node_broken"
    if _LOCAL_CLONE_STEP.search(text):
        return "other"
    words = re.search(r"clone failed: [^:\n]{1,253}: (.*)", text)
    if words is not None:
        return next((cause for said, cause in _NODE_CAUSES.items() if words.group(1).startswith(said)), "other")
    return "access" if _ACCESS_HINT.search(text) else "other"


def headline(detail: Optional[str]) -> str:
    """What failed and what to do, in one or two sentences; "" when `detail` is no clone or checkout refusal. A task's
    clone refusal is worded by `clone_refusal_sentence` instead, which names the starter whose login it used."""
    text = (detail or "").strip()
    found = _CHECKOUT_REFUSAL.match(text)
    if found is None:
        return ""
    if found.group("branch") is not None:
        return (f"Integration branch '{found.group('branch')}' does not exist in the repository and could not be "
                f"created from its default branch.")
    what = "clone the repository" if found.group("clone") else "check out the integration branch"
    if _clone_cause(text) == "node_broken":
        return (f"The node could not {what}: its install is broken (git credential helper missing). "
                f"Re-run the node install, then Retry.")
    access = _clone_cause(text) in ("auth", "access") if found.group("clone") else _ACCESS_HINT.search(text)
    hint = " Check the project's repository URL and its Git access credential." if access else ""
    return f"The node could not {what}.{hint}"


def is_clone_refusal(detail: Optional[str]) -> bool:
    """A clone or mirror-fetch refusal (`_CHECKOUT_REFUSAL`'s `clone` group), the kind the starter's login decides."""
    found = _CHECKOUT_REFUSAL.match((detail or "").strip())
    return found is not None and found.group("clone") is not None


def asks_starter(detail: Optional[str]) -> bool:
    """A clone the node refused because the forge says the repository is not there for this login (`slot_clone`'s
    "the repository was not found"; an older node's "Repository not found"): the starter cannot see it, and no
    retry changes that, so the refusal asks them (`access_question`). A refused or missing credential is not this."""
    text = (detail or "").strip()
    if not is_clone_refusal(text) or _HELPER_MISSING.search(text) or _LOCAL_CLONE_STEP.search(text):
        return False
    words = re.search(r"clone failed: [^:\n]{1,253}: (.*)", text)
    if words is not None:
        return words.group(1).startswith("the repository was not found")
    return "Repository not found" in text


def access_question(session: Session, task: Optional[Task], detail: Optional[str]) -> str:
    """"<First name>, you don't have access to <repo> in <forge>. Start anyway?" for a task whose clone `detail`
    `asks_starter`; "" when the login is no person's (a Jaah-made repository's, or no starter) or it cannot be
    worded, so the caller raises the ordinary round."""
    if task is None or not asks_starter(detail):
        return ""
    try:
        from app import jaah_repos  # noqa: PLC0415 — only this round needs it

        project = session.scalars(select(Project).where(Project.id == task.project_id)).first()
        repo = git_access.project_repo(project, retry="then Start the task again")
        if jaah_repos.runner_app_repo(session, project, repo) is not None:
            return ""
        person_id = git_access.git_person_for_task(session, task)
        if person_id is None:
            return ""
        person = session.scalars(select(AdminUser).where(AdminUser.id == person_id)).first()
        first = ((person.first_name or "").split() or [""])[0] if person is not None else ""
        you = f"{first}, you" if first else "You"
        return f"{you} don't have access to {repo.path} in {FORGE_LABELS[repo.forge]}. Start anyway?"
    except Exception:  # noqa: BLE001 — a wording failure falls back to the ordinary round
        log.warning("checkout-gate: could not word task %s's access question", getattr(task, "id", None),
                    exc_info=True)
        return ""


def _person_name(session: Session, person_id) -> str:
    """`person_id`'s name, else their email (`identities.display_name`)."""
    from app.identities import display_name  # noqa: PLC0415 — light, but kept off this module's import graph

    person = session.scalars(select(AdminUser).where(AdminUser.id == person_id)).first()
    return (display_name(person) or person.email or "") if person is not None else ""


def clone_refusal_sentence(session: Session, task: Task, detail: Optional[str], *, parked: bool = False,
                           node_id: Optional[UUID] = None, refused_at: Optional[datetime] = None) -> str:
    """`task`'s clone refusal `detail` in words a person acts on, ending in "." — "" when `detail` is no clone
    refusal (`is_clone_refusal`) or the wording itself failed (logged: a sentence must never cost an ack or a board
    read, and every caller falls back to the node's own words). A refused login names the task's starter, whose
    login the clone used (`git_access.git_person_for_task`), and the fix; a Jaah-made repository's login is the
    runner app's (`jaah_repos.runner_app_repo`), no person's. A network or timeout failure says it retries, or,
    `parked` (a checkout round, `raise_round`), asks for a Retry once the forge answers. Given the refusing
    `node_id` and when it refused (`refused_at`), a refused login that has cloned the project on another node since
    (`_cloned_elsewhere`) is worded as that node's fault, not the person's. `dispatch_wait` prefixes "Waiting: "."""
    text = (detail or "").strip()
    if not is_clone_refusal(text):
        return ""
    try:
        return _clone_refusal_words(session, task, text, parked, node_id, refused_at)
    except Exception:  # noqa: BLE001 — see the docstring: the caller shows the node's words instead
        log.warning("checkout-gate: could not word task %s's clone refusal", getattr(task, "id", None), exc_info=True)
        return ""


def _cloned_elsewhere(session: Session, task: Task, person_id, node_id: UUID, refused_at: datetime) -> Optional[str]:
    """The name of node `node_id` when, after its refusal at `refused_at`, a task of `task`'s project that
    `person_id` started had a slot built (its `lease_slot` order `done`) on ANOTHER node: the same login cloned
    there since, so this node's refusal is the node's, not the login's. None otherwise. Two indexed reads: the
    person's `_ELSEWHERE_TASKS` newest tasks of the project, then their orders by `ix_slot_intents_lease_slot_task`."""
    from app.models.nodes import Node  # noqa: PLC0415 — only this wording needs it

    theirs = [str(task_id) for task_id in session.scalars(
        select(Task.id).where(Task.project_id == task.project_id, Task.started_by_id == person_id,
                              Task.updated_at >= refused_at)
        .order_by(Task.updated_at.desc()).limit(_ELSEWHERE_TASKS)
    )]
    if not theirs:
        return None
    task_key = SlotIntent.payload["task_id"].astext
    built = session.scalars(
        select(SlotIntent.id).where(
            SlotIntent.verb == fleet_verdicts.VERB_LEASE_SLOT, task_key.in_(theirs),
            SlotIntent.created_at > refused_at, SlotIntent.node_id != node_id,
            SlotIntent.outcome == fleet_verdicts.OUTCOME_DONE).limit(1)
    ).first()
    if built is None:
        return None
    return session.scalars(select(Node.name).where(Node.id == node_id)).first()


def _clone_refusal_words(session: Session, task: Task, text: str, parked: bool,
                         node_id: Optional[UUID] = None, refused_at: Optional[datetime] = None) -> str:
    from app import jaah_repos  # noqa: PLC0415 — only a refused login needs it

    cause = _clone_cause(text)
    if cause == "node_broken":
        return "The node's git credential helper is missing. Re-run the node install."
    if cause == "other":
        said = " ".join((fleet_verdicts.redact_node_detail(text) or "").split()).rstrip(".")
        return f"The node could not clone the repository. The node said: {said}." if said else (
            "The node could not clone the repository and gave no reason.")
    project = session.scalars(select(Project).where(Project.id == task.project_id)).first()
    try:
        repo = git_access.project_repo(project, retry="then Retry" if parked else "then Start the task again")
    except git_access.GitAccessError as exc:
        return str(exc)
    forge = FORGE_LABELS[repo.forge]
    if cause in ("network", "timeout"):
        failed = "could not be reached" if cause == "network" else "did not answer in time"
        return f"{forge} {failed}. Retry once it answers." if parked else f"{forge} {failed}; retrying."
    if jaah_repos.runner_app_repo(session, project, repo) is not None:
        return f"Jaah's runner app can't see {repo.path}. Contact support."
    person_id = git_access.git_person_for_task(session, task)
    if person_id is None:
        return f"Nobody started this task, so no {forge} account can clone {repo.path}. Start it to run it as you."
    who = _person_name(session, person_id) or "The starter"
    where = git_access.profile_path(repo.forge)
    try:
        git_access.resolve_for_user(session, person_id, repo)
    except git_access.GitAccessError:
        return f"{who} has no {forge} account for {repo.path}. Connect one under {where}, or let a teammate start it."
    node = (_cloned_elsewhere(session, task, person_id, node_id, refused_at)
            if node_id is not None and refused_at is not None else None)
    if node:
        return (f"Node {node} can't clone {repo.path} with {who}'s {forge} login, which works on other nodes. "
                "Reset the task to run it elsewhere.")
    if cause == "auth":
        return f"{forge} refused {who}'s login. Reconnect it under {where}."
    return (f"{who}'s {forge} account can't see {repo.path}. Connect one with access under {where}, "
            "or let a teammate start it.")


def raise_round(session: Session, task_id: int, project_id: int, detail: str, refusals: int) -> bool:
    """Park task `task_id` on a checkout round: `ready|active → awaiting_response` and one open `task_questions`
    row. Commits. False when the task was not `ready`/`active` (settled, stopped, or already asking). A clone round
    is worded about the task's starter (`clone_refusal_sentence`) and marked for `retry_refusal`'s preflight."""
    # Paths, URL credentials and secret shapes (`redact_node_detail`): the round may reach Slack.
    said = fleet_verdicts.redact_node_detail(detail)
    clone = is_clone_refusal(detail)
    task = session.scalars(select(Task).where(Task.id == task_id)).first() if clone else None
    context: dict[str, object] = {CP_GATE_KEY: GATE, "project_id": project_id}
    if clone:
        context["clone"] = True
    ask = access_question(session, task, detail)
    if ask:
        context["access"] = True
        row = TaskQuestion(task_id=task_id, question=ask, options=ACCESS_OPTIONS, multi_select=False, header=None,
                           status="open", guard_context=context)
    else:
        cause = clone_refusal_sentence(session, task, detail, parked=True) if task is not None else headline(detail)
        question = (f"{cause} The task did not start; the node refused {"once" if refusals == 1 else f"{refusals} times the same way"}.\n\n"
                    f"The node said: {said}")
        row = TaskQuestion(task_id=task_id, question=question, options=OPTIONS, multi_select=False, header=HEADER,
                           status="open", guard_context=context)
        ask = question
    # Flushed first so the transition's own commit carries it: a parked task never lacks its round.
    session.add(row)
    session.flush()
    if await_response_on_wedged_dispatch(task_id, ask, session) is None:
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
    if not _is_gate_round(row):
        return answer
    if (row.guard_context or {}).get("access"):
        # Only its two answers, matched without case: a typed Retry or branch would skip their own checks.
        chosen = next((label for label in (START_ANYWAY, DONT_START)
                       if answer.strip().rstrip(".!").lower() == label.lower()), None)
        if chosen is None:
            raise HTTPException(status_code=422, detail=f"Answer {START_ANYWAY} or {DONT_START}.")
        if chosen == START_ANYWAY:
            refusal = slot_allocation.folder_slot_refusal(session, task.project_id) if task.project_id else ""
            if refusal:
                raise HTTPException(status_code=409, detail=refusal)
        return chosen
    if answer.strip() == RETRY:
        refusal = retry_refusal(session, task, row)
        if refusal:
            raise HTTPException(status_code=409, detail=refusal)
        return answer
    if answer.strip() != CHANGE_BRANCH:
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
    require_project_key(acting, permission_key("projects", "edit"), project.id,
                        detail="Changing the integration branch needs projects:edit.")
    apply_project_update(session, project, {"integration_branch": branch}, user, acting)
    return answer


def retry_refusal(session: Session, task: Task, row: TaskQuestion) -> str:
    """Why a `RETRY` of clone round `row` would only be refused again, or "" to let it through.

    Asks the forge (`git_reach.write_reach`, past its cache) whether the task's starter's login — the one its clone
    uses (`git_access.resolve_for_task`) — can see the repository; only a plain no that blocks reading too
    (`Reach.blocks_read`) refuses, so a read-only starter, an outage or another forge retries as before. A task with
    no starter or no usable selection refuses with `git_access`'s own sentence;
    a token that cannot be renewed right now lets the Retry through. Bounded by `git_reach`'s per-request timeout;
    runs once per Retry, off the event loop. Checks the project's PRIMARY repository only: a non-primary
    repository's refusal retries unchecked."""
    context = row.guard_context or {}
    # `clone_node_id`: a round raised before the `clone` marker.
    if not (context.get("clone") or context.get("clone_node_id")) or task.project_id is None:
        return ""
    project = session.scalars(
        select(Project).where(Project.id == task.project_id, Project.org_id == task.org_id)
    ).one_or_none()
    if project is None or not project.has_repo:
        return ""
    try:
        repo = git_access.project_repo(project, retry="then Retry")
        credential = git_access.resolve_for_task(session, task, repo)
    except git_access.GitAccessError as exc:
        return f"Not retried: {exc}"
    try:
        token = git_access.token_for(session, credential)
    except git_access.GitAccessError:
        return ""  # a renewal that failed now may not later: the node's own ask decides
    # The GitHub call off the event loop: an async route runs this inside `run_sync` (`db_async.wait_off_loop`).
    from app.db_async import wait_off_loop  # noqa: PLC0415 — the route-session seam, only this call needs it

    reach = wait_off_loop(session, git_reach.write_reach, credential, token, repo, fresh=True)
    if reach.verdict != "no" or not reach.blocks_read:
        return ""
    fix = f" Fix it at {reach.fix_url}." if reach.fix_url else ""
    person_id = git_access.git_person_for_task(session, task)
    who = _person_name(session, person_id) if person_id is not None else ""
    whose = f" The clone uses {who}'s {FORGE_LABELS[repo.forge]} account." if who else ""
    return f"Not retried — it would fail the same way. {reach.problem}{fix}{whose}"


def settle(session: Session, task: Task, rows: list[TaskQuestion]) -> bool:
    """Enact a resolved checkout round: `CANCEL` resets the task to draft (and commits, then sends the slot
    intents the reset minted), `RETRY` and `CHANGE_BRANCH` are `answer`'s `awaiting_response → ready` for a fresh
    dispatch, and a dismissal leaves the task parked for Reset or Start. False when none of `rows` is a checkout
    round, so the caller hands them to a runner as usual."""
    row = next((r for r in rows if _is_gate_round(r)), None)
    if row is None:
        return False
    choice = (row.chosen_resolution or "").strip()
    access = bool((row.guard_context or {}).get("access"))
    if access:
        # The no-access round's answers in the ordinary round's terms: No is Cancel; Yes is handled below.
        choice = {DONT_START: CANCEL}.get(choice, choice)
    without_repo = access and choice == START_ANYWAY
    if choice not in (RETRY, CHANGE_BRANCH, CANCEL) and not without_repo:
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
            github_synced_at=utcnow(),
            **({"run_without_repo": True} if without_repo else {}),
            **({"dispatch_attempts": 0} if resume.resets_attempts else {}),
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    issued = _free_git_slot(session, task) if moved and without_repo else []
    if moved:
        _retire_parked_orders(session, task.id)
    emit_console_event(session, entity="task", id=task.id, action=resume.target if moved else task.state,
                       record_transition=bool(moved))
    if issued:
        from app.routes.tasks.slots import send_minted_intents  # noqa: PLC0415 — see the CANCEL arm

        session.commit()
        send_minted_intents(issued)
    log.info("task %s: checkout round answered %r — %s", task.id, row.chosen_resolution,
             f"back to {resume.target} for a fresh dispatch" + (" without the repository" if without_repo else "")
             if moved else f"left {task.state}")
    return True


def _free_git_slot(session: Session, task: Task) -> list:
    """The no-access round's Yes: free the task's git worktree — its clone never landed, so nothing is lost; the
    node wipes it, as on a Reset — so the next lease gives the task a folder slot (`tasks.run_without_repo`).
    Does not commit; returns the minted intents for the caller to send after its commit."""
    from app.routes.tasks.slots import _release_task_slots, abandon_task_slots  # noqa: PLC0415 — see `settle`

    reason = f"task {task.id} starts without the repository: its starter has no access to it"
    _, issued = abandon_task_slots(task.id, session, reason=reason)
    _release_task_slots(task.id, session, reason=f"released: {reason}")
    return issued


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
