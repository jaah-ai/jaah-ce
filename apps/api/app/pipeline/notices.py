"""Notices of a task step's approval rules (architecture §4.6.1), each through `user_notify.deliver`:

- `step_approval_timeout` — the step's approval timeout ran out and its press was withdrawn: to whoever asked (the
  task's creator and starter, and whoever pressed it);
- `step_rejected` — an approver rejected it: to whoever asked, naming the approver and their comment;
- `step_approval_reminder` — the timeout ran out on a step that keeps waiting: to every approver who has not
  approved, once per timeout period (the period is stamped on the first press, `pipeline_approvals.reminded_at`).

The first two are one-shot: the press is consumed in the same commit, so nothing is left to send them again. The
catalog files them under the release events they mirror (`app/notification_catalog.py`'s `EVENT_ALIASES`). Release
notices are `app/releases/notify.py`'s. Nothing here commits.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import alert_relay, email_notify, user_notify
from app.actors import actor_names
from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import scoped_task_select
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task
from app.pipeline import gate as gate_mod
from app.pipeline.rules import StepRules
from app.releases import notify as release_notify

STEP_TIMEOUT = "step_approval_timeout"
STEP_REJECTED = "step_rejected"
STEP_REMINDER = "step_approval_reminder"
#: How long a quoted comment may run in a notice.
COMMENT_SHOWN = 200


def _what(session: Session, step: gate_mod.Step, subject: gate_mod.Subject) -> tuple[str, Optional[str]]:
    """The item and step as a notice names them, and the page it opens."""
    if subject.task_id is not None:
        title = session.scalar(select(Task.title).where(Task.id == subject.task_id)) or f"Task #{subject.task_id}"
        return f"{title} · {step.target.name}", f"/tasks/{subject.task_id}"
    return f"{(subject.sha or '')[:10]} · {step.target.name}", None


def _send(session: Session, project: Project, people: Iterable[AdminUser], kind: str, text: str,
          path: Optional[str], subject: gate_mod.Subject) -> int:
    link = email_notify.console_path_link(path or release_notify.tab_path(project.id))
    event = alert_relay.message(kind, text, payload={"console_url": link})
    about = {"project_id": project.id, "task_id": subject.task_id, "sha": subject.sha}
    count = 0
    for person in people:
        user_notify.deliver(session, person, event, event_name=kind, subject=about)
        count += 1
    return count


def _members(session: Session, project: Project, ids: Iterable[UUID]) -> list[AdminUser]:
    found = (release_notify._member_in(session, user_id, project.org_id) for user_id in ids)
    return list({m.id: m for m in found if m is not None}.values())


def step_ended(session: Session, project: Project, step: gate_mod.Step, subject: gate_mod.Subject,
               asked: set[UUID], kind: str, *, by: Optional[AdminUser] = None, comment: Optional[str] = None) -> int:
    """Tell whoever `asked` that the wait ended without opening: timed out (`STEP_TIMEOUT`) or rejected by `by`
    (`STEP_REJECTED`), never the rejecter themself."""
    what, path = _what(session, step, subject)
    if kind == STEP_REJECTED:
        name = (actor_names(session, {by.id}).get(by.id) if by is not None else None) or "an approver"
        text = f"Rejected by {name}: {what}"
        if comment:
            text += f" — “{comment[:COMMENT_SHOWN]}”"
    else:
        text = f"Approval timed out: {what}"
    # The notice names the task: only someone who may still see it is told.
    people = [p for p in _members(session, project, asked)
              if (by is None or p.id != by.id) and _sees(session, principal_for_user(session, p), subject)]
    return _send(session, project, people, kind, text, path, subject)


def _sees(session: Session, principal: Optional[Principal], subject: gate_mod.Subject) -> bool:
    if subject.task_id is None:
        return True
    return session.scalar(scoped_task_select(principal).where(Task.id == subject.task_id)
                          .with_only_columns(Task.id)) is not None


def step_reminder(session: Session, project: Project, step: gate_mod.Step, subject: gate_mod.Subject) -> int:
    """Remind every approver of the step who has not approved `subject` (nor asked for it, while R2 keeps askers
    out)."""
    approved = {a.user_id for a in gate_mod.approvals_of(session, step, subject)}
    skip = approved | (gate_mod.askers(session, step, subject) if StepRules.of(step.target).requester_cant_approve
                       else set())
    members = gate_mod.approver_candidates(session, project, step.target, step.action)
    people = []
    for member in members:
        if member.id in skip:
            continue
        principal = principal_for_user(session, member)
        # The notice names the task: only an approver who may see it is told.
        if gate_mod.may_approve(principal, project.id, step) and _sees(session, principal, subject):
            people.append(member)
    what, path = _what(session, step, subject)
    return _send(session, project, people, STEP_REMINDER, f"Approval waiting: {what}", path, subject)
