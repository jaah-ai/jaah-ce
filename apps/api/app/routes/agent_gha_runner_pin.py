"""The agent-facing GitHub Actions runner pin: a runner sets the three runner-pin operator settings together
(`gha_runner_version`, `gha_runner_sha256_x64`, `gha_runner_sha256_arm64`) as the human its task acts for.

The weekly System Maintenance automation's task checks GitHub for a newer runner release, asks the operator
(`AskUserQuestion`, a `task_questions` round), and on their yes writes the pin here. Who acts: the launcher
(`routes/agent/auth.py::launcher_principal`), who must be the operator — `routes/settings.py::_operator_actor`'s
verdict, `is_operator_user`. The write is the Settings PUT's own writer (`operator_settings.writes.apply_overrides`,
then the cache invalidate), so validation, storage and cache behave as a Settings save; the request middleware
records the call like any other agent write.

THE ONE LOOP-GUARD EXCEPTION (`_require_human_started_or_operator_yes`): every other settings write refuses a
machine-started chain (`require_human_started`). This door admits one when the calling task itself holds an
answered question round that an operator answered within `_ANSWER_WINDOW` (and after the task's last Reset) by
choosing exactly the option `yes_option(version)` on a question that shows both digests being written — so the yes
covers all three values, and a "no", a reworded option or a yes to another release never counts — because
the operator decided that their yes in the task's Q&A trail authorizes exactly this one write. The answer is a
person's by construction: only the console, Slack and tracker answer paths resolve a round, each as a member
(`routes/tasks/answers.py::_record_answered_round`), and a control-plane round (`guard_context`) does not count.
Nothing else is loosened. Auth is `_check_token`, the caller held by this node and live (`live_agent_caller`).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.clock import utcnow
from app.db_async import on_session
from app.entity_shape import StrictModel, conflict_as_409, refused_as
from app.models.identity import AdminUser
from app.models.tasks import Task, TaskQuestion
from app.operator_settings import cache as settings_cache
from app.operator_settings import errors as settings_errors
from app.operator_settings import reads as settings_reads
from app.operator_settings import writes as settings_writes
from app.task_ancestry import machine_origin
from app.tenancy.ids import is_operator_user

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`).
from app.routes.agent.auth import AgentDoor, caller_and_launcher

log = logging.getLogger("agent-gha-runner-pin")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator.
router = APIRouter(tags=["agent-gha-runner-pin"])

#: The three knobs this door writes, together (contract names).
VERSION_KEY = "gha_runner_version"
SHA256_X64_KEY = "gha_runner_sha256_x64"
SHA256_ARM64_KEY = "gha_runner_sha256_arm64"

#: How recent an operator's answer must be to stand for a machine-started chain's write: long enough for a runner
#: to act on the answer it was handed, short enough that last week's yes does not authorize this week's pin.
_ANSWER_WINDOW = timedelta(hours=24)

_VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class GhaRunnerPinIn(StrictModel):
    """The runner release to pin and its two Linux tarball digests (the release body's `BEGIN SHA linux-x64` and
    `BEGIN SHA linux-arm64` lines)."""

    version: str = Field(pattern=_VERSION_PATTERN, description="e.g. 2.338.2, no leading v")
    sha256_x64: str = Field(pattern=_SHA256_PATTERN, description="64 lowercase hex")
    sha256_arm64: str = Field(pattern=_SHA256_PATTERN, description="64 lowercase hex")


class GhaRunnerPinOut(StrictModel):
    """The three values as stored after the write."""

    version: str
    sha256_x64: str
    sha256_arm64: str


def yes_option(version: str) -> str:
    """The one option label whose choice authorizes pinning `version` from a machine-started task. Fixed here, not
    by the runner, so no option a runner words ("Skip 2.339.0") can pass for a yes."""
    return f"Pin runner v{version}"


def _approves(question: str | None, answer: str | None, pin: GhaRunnerPinIn) -> bool:
    """Whether the operator chose exactly `yes_option` on a question that showed both digests being written."""
    shown = question or ""
    return (answer or "").strip() == yes_option(pin.version) and pin.sha256_x64 in shown and pin.sha256_arm64 in shown


def _operator_answered(session: Session, task: Task, pin: GhaRunnerPinIn) -> bool:
    """Whether `task` holds a question round an operator answered within `_ANSWER_WINDOW`, after its last Reset,
    approving exactly this pin (`_approves`).

    The task's own rounds only (not an ancestor's), runner-asked only (`guard_context` NULL), and the answerer a
    live, non-system operator (`is_operator_user`): the same verdict this door asks of the launcher."""
    cutoff = utcnow() - _ANSWER_WINDOW
    if task.artifacts_hidden_before is not None and task.artifacts_hidden_before > cutoff:
        # Reset stamps this; a yes given to a superseded attempt does not carry over.
        cutoff = task.artifacts_hidden_before
    rounds = session.execute(
        select(TaskQuestion.resolved_by_id, TaskQuestion.question, TaskQuestion.chosen_resolution).where(
            TaskQuestion.task_id == task.id,
            TaskQuestion.status == "answered",
            TaskQuestion.guard_context.is_(None),
            TaskQuestion.resolved_by_id.is_not(None),
            TaskQuestion.resolved_at > cutoff,
        )
    ).all()
    for user_id, question, answer in rounds:
        if not _approves(question, answer, pin):
            continue
        user = session.get(AdminUser, user_id)
        if user is not None and not user.is_system and user.disabled_at is None and is_operator_user(user):
            return True
    return False


def _require_human_started_or_operator_yes(session: Session, caller: Task, pin: GhaRunnerPinIn) -> None:
    """`require_human_started`, with this door's single exception (the module docstring says why): a
    machine-started chain passes only when an operator answered one of the calling task's own question rounds
    recently, approving exactly this pin (`_operator_answered`). 403 otherwise, naming what is missing."""
    reason = machine_origin(session, caller.id)
    if reason is None:
        return
    if _operator_answered(session, caller, pin):
        log.info("gha runner pin: machine-started task=%s (%s) admitted on an operator's answer", caller.id, reason)
        return
    log.warning("gha runner pin: refused task=%s (%s; no operator answer)", caller.id, reason)
    raise HTTPException(
        status_code=403,
        detail=(
            f"setting the runner pin from a machine-started task needs an operator's yes first, and {reason}. "
            "Ask the operator with AskUserQuestion: show both SHA-256 digests in the question and offer the "
            f"option labelled exactly '{yes_option(pin.version)}'; once they choose it, call this again."
        ),
    )


@router.post("/api/agent/tasks/settings/gha-runner-pin", response_model=GhaRunnerPinOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def set_gha_runner_pin(session: Session, door: AgentDoor, body: GhaRunnerPinIn) -> GhaRunnerPinOut:
    """Set the runner version and both digests in one Settings write, as the operator the task acts for."""
    caller, user, _principal = caller_and_launcher(session, *door)
    if not is_operator_user(user):
        raise HTTPException(
            status_code=403,
            detail="the runner pin is a fleet-wide setting only an operator may change, and this task acts for "
                   "someone else. Name the release in your report.",
        )
    _require_human_started_or_operator_yes(session, caller, body)
    values = {VERSION_KEY: body.version, SHA256_X64_KEY: body.sha256_x64, SHA256_ARM64_KEY: body.sha256_arm64}
    with refused_as(422, settings_errors.OperatorSettingsError, session=session):
        # Two saves raced to create the same key: a 409, and nothing of this one landed (the Settings PUT's rule).
        with conflict_as_409(session, "Changed by someone else just now. Call this again."):
            settings_writes.apply_overrides(session, values, org=user.org_id)
    settings_cache.invalidate_cache()
    log.info("gha runner pin: task=%s set version=%s (as operator=%s)", caller.id, body.version, user.id)
    stored = settings_reads.overrides(session)
    return GhaRunnerPinOut(
        version=stored.get(VERSION_KEY, ""),
        sha256_x64=stored.get(SHA256_X64_KEY, ""),
        sha256_arm64=stored.get(SHA256_ARM64_KEY, ""),
    )
