"""Release notices (architecture §8), each through `user_notify.deliver` and each sent once. The catalog events are
the `deploys` group of `app/notification_catalog.py`:

- `release_approval` — "Approve release: <project>" to everyone who may approve a release awaiting approval.
- `release_live` — "Live: <task title>" (or "Live: N tasks — …") to each included task's creator, once per
  release: one notice naming all of that person's tasks the release carried.
- `release_failed` — "Release failed: <project>. <action>" to the requester, once per failure.
- `release_rolled_back` — "Rolled back: <stage> of <project> is back on <version>" to whoever pressed Roll back and
  the requester of the release it undid, once per roll back that reached its stage.
- `release_approval_timeout` — "Approval timed out: <project>" to the requester of a release the step's approval
  timeout canceled (R3.2), once per release.
- `release_rejected` — "Rejected by <name>: <project>" (and their comment) to the requester, once per release.
- `release_approval_reminder` — "Approval waiting: <project>" to every approver who has not approved a release still
  waiting past its timeout ("keep waiting"), once per timeout period (`subject` names the period).

Every notice also lands in each recipient's bell in the project's org: one `release_notices` row (`routes/inbox.py`'s
`release_*` kinds), written before the channels and whatever they do, so a person with every channel off still has
it. Recipients are resolved by membership: a person whose home org is another one is told through their membership
row in the project's org; a disabled or system member is told nothing.

"Sent" is read back off the `notification_sent` activity rows (`subject` names the project, release or task, each
lookup bounded by when its subject began), so a tick that runs again never sends twice; the bell row's unique
`(user_id, kind, subject_key)` makes a retried write a no-op too. The tick sends them last, in a commit of their own
(`tick.notices`), since `deliver` sends at once and only its row waits for the commit. Nothing here commits; the tick
does.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import alert_relay, email_notify, identities, notify_targets, notify_text, trigger_notices, user_notify
from app.authz.principal import principal_for_user
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import Release, ReleaseApproval, ReleaseNotice, ReleaseStage
from app.releases import access
from app.tenancy import seam

log = logging.getLogger(__name__)

APPROVAL = "release_approval"
LIVE = "release_live"
FAILED = "release_failed"
APPROVAL_TIMEOUT = "release_approval_timeout"
APPROVAL_REMINDER = "release_approval_reminder"
REJECTED = "release_rejected"
ROLLED_BACK = "release_rolled_back"
#: How long a quoted rejection comment may run in a notice.
COMMENT_SHOWN = 200

#: `failure.action` → what the notice tells the requester to do.
ACTION_LABELS = {"retry": "Retry", "connect": "Connect releases", "open_pr": "Open the PR", "open_run": "Open the run"}


def tab_path(project_id: int) -> str:
    """The project's pipeline page (`/tasks?project=`, as `apps/web/src/lib/taskDeepLink.ts` forms it): every
    notice's link."""
    return f"/tasks?project={project_id}"


def sent(session: Session, org_id: UUID, event_name: str, subject: dict[str, Any],
         since: Optional[datetime] = None) -> bool:
    """Whether a `event_name` notice about `subject` was sent (to anyone) in `org_id`, at or after `since`."""
    stmt = select(ActivityEvent.id).where(
        ActivityEvent.org_id == org_id, ActivityEvent.kind == notify_targets.NOTIFICATION_SENT,
        ActivityEvent.data["event_name"].astext == event_name,
        *(ActivityEvent.data["subject"][key].astext == str(value) for key, value in subject.items()),
    )
    if since is not None:
        stmt = stmt.where(ActivityEvent.created_at >= since)
    return session.scalar(stmt.limit(1)) is not None


def _members(session: Session, org_id: UUID) -> list[AdminUser]:
    return list(session.scalars(select(AdminUser).where(
        AdminUser.org_id == org_id, AdminUser.disabled_at.is_(None), AdminUser.is_system.is_(False),
    ).order_by(AdminUser.id)))


def _member_in(session: Session, admin_user_id: UUID, org_id: UUID) -> Optional[AdminUser]:
    """The person behind `admin_user_id` as a member of `org_id`: that row itself, else their membership there (one
    identity, several orgs); None when they are not a member, or the member is disabled or the system. The tick's
    session is bound to the project's org, so the row itself is read under the identity escape: a home-org row
    elsewhere would otherwise read as nobody."""
    with seam.identity_scope():
        row = session.scalar(select(AdminUser).where(AdminUser.id == admin_user_id))
    if row is not None and row.org_id != org_id:
        row = identities.membership_in(session, row.identity_id, org_id)
    if row is None or row.disabled_at is not None or row.is_system:
        return None
    return row


def _stamp(at: Optional[datetime]) -> str:
    return at.isoformat() if at is not None else ""


def approvers(session: Session, project: Project, stage: ReleaseStage, release: Release) -> list[AdminUser]:
    return [u for u in _members(session, project.org_id)
            if access.may_approve(principal_for_user(session, u), project.id, stage, release)]


def _send(session: Session, project: Project, people: Iterable[AdminUser], event_name: str, text: str,
          link: Optional[str], subject: dict[str, Any], since: Optional[datetime]) -> int:
    """The bell row, then the person's own channels, for each of `people`. `since` (the period or request the notice
    belongs to) completes the bell row's dedupe text."""
    event = alert_relay.message(event_name, text, payload={"console_url": link})
    subject_key = "|".join([*(f"{key}={value}" for key, value in sorted(subject.items())), _stamp(since)])
    count = 0
    for person in people:
        session.execute(insert(ReleaseNotice).values(
            org_id=project.org_id, user_id=person.id, project_id=project.id, kind=event_name,
            subject_key=subject_key, summary={"text": text, "href": tab_path(project.id)},
        ).on_conflict_do_nothing(index_elements=["user_id", "kind", "subject_key"]))
        user_notify.deliver(session, person, event, event_name=event_name, subject=subject)
        count += 1
    return count


def _tab_link(project: Project) -> Optional[str]:
    return email_notify.console_path_link(tab_path(project.id))


def approval(session: Session, project: Project, stage: ReleaseStage, release: Release) -> int:
    subject = {"release_id": release.id}
    if sent(session, project.org_id, APPROVAL, subject, release.requested_at):
        return 0
    return _send(session, project, approvers(session, project, stage, release), APPROVAL,
                 f"Approve release: {project.name}", _tab_link(project), subject, release.requested_at)


def reminder(session: Session, project: Project, stage: ReleaseStage, release: Release, period: int) -> int:
    """Remind every approver who has not approved `release` (`stage`: its snapshot rule) that it still waits: once
    per timeout `period` (1 = the first timeout ran out)."""
    subject = {"release_id": release.id, "period": period}
    if sent(session, project.org_id, APPROVAL_REMINDER, subject, release.requested_at):
        return 0
    from app.pipeline import gate as pipeline_gate  # noqa: PLC0415 — the gate imports this package
    from app.releases import pipeline as setup  # noqa: PLC0415

    approved = {str(u) for u in session.scalars(
        select(ReleaseApproval.user_id).where(ReleaseApproval.release_id == release.id))}
    # As a task step's reminder: an operator is reminded only when named (`gate.approver_candidates`).
    people = [u for u in pipeline_gate.approver_candidates(session, project, stage, setup.RELEASE)
              if str(u.id) not in approved
              and access.may_approve(principal_for_user(session, u), project.id, stage, release)]
    return _send(session, project, people, APPROVAL_REMINDER, f"Approval waiting: {project.name}",
                 _tab_link(project), subject, release.requested_at)


def ended(session: Session, project: Project, release: Release) -> int:
    """Tell the requester their release ended waiting for approvals: timed out or rejected (`release.cancellation`);
    once per release."""
    cancellation = release.cancellation or {}
    kind = {"timeout": APPROVAL_TIMEOUT, "rejected": REJECTED}.get(str(cancellation.get("kind")))
    subject = {"release_id": release.id}
    if kind is None or release.requested_by_id is None or sent(session, project.org_id, kind, subject,
                                                               release.requested_at):
        return 0
    requester = _member_in(session, release.requested_by_id, project.org_id)
    if requester is None or str(requester.id) == str(cancellation.get("by_id")):
        return 0  # never told of their own rejection
    if kind == REJECTED:
        by = _by_name(session, cancellation.get("by_id"))
        text = f"Rejected by {by}: {project.name}"
        if cancellation.get("comment"):
            text += f" — “{str(cancellation['comment'])[:COMMENT_SHOWN]}”"
    else:
        text = f"Approval timed out: {project.name}"
    return _send(session, project, [requester], kind, text, _tab_link(project), subject, release.requested_at)


def _by_name(session: Session, user_id: Any) -> str:
    from app.actors import actor_names  # noqa: PLC0415

    try:
        uid = UUID(str(user_id))
    except ValueError:
        return "an approver"
    return actor_names(session, {uid}).get(uid) or "an approver"


#: How many titles a several-task "Live" notice names before "and N more", and how long each may be.
LIVE_TITLES_SHOWN = 3
LIVE_TITLE_MAX = 80


def live_text(titles: list[str]) -> str:
    """"Live: <title>" for one task; "Live: N tasks — a; b; c and N more" for several."""
    if len(titles) == 1:
        return f"Live: {titles[0]}"
    shown = "; ".join(notify_text._clip(title, LIVE_TITLE_MAX) for title in titles[:LIVE_TITLES_SHOWN])
    rest = len(titles) - LIVE_TITLES_SHOWN
    return f"Live: {len(titles)} tasks — {shown}" + (f" and {rest} more" if rest > 0 else "")


def live(session: Session, project: Project, release: Release,
         tasks: Iterable[tuple[int, str, Optional[UUID]]]) -> int:
    """One "Live" notice per release to each creator of its tasks (`(task_id, title, created_by_id)`), naming their
    tasks: a release of ten of someone's tasks tells them once, not ten times. A single task links to its page,
    several to the project's pipeline page."""
    mine: dict[UUID, tuple[AdminUser, list[tuple[int, str]]]] = {}
    members: dict[UUID, Optional[AdminUser]] = {}
    for task_id, title, creator_id in tasks:
        if creator_id is None:
            continue
        if creator_id not in members:
            members[creator_id] = _member_in(session, creator_id, project.org_id)
        creator = members[creator_id]
        if creator is not None:
            mine.setdefault(creator.id, (creator, []))[1].append((task_id, title))
    count = 0
    for creator, owned in mine.values():
        subject = {"release_id": release.id, "user_id": str(creator.id)}
        if sent(session, project.org_id, LIVE, subject, release.requested_at) or all(
            # Announced per task before notices were per release: not told twice. Removable once no release from
            # before that change can still be in the tick's window.
            sent(session, project.org_id, LIVE, {"task_id": task_id}, release.requested_at) for task_id, _ in owned
        ):
            continue
        link = (email_notify.console_path_link(f"/tasks/{owned[0][0]}") if len(owned) == 1
                else _tab_link(project))
        count += _send(session, project, [creator], LIVE, live_text([title for _, title in owned]), link,
                       subject, release.requested_at)
    return count


def failed(session: Session, project: Project, release: Release) -> int:
    """"Release failed" to the requester, naming the one thing to do; once per failure (a retried release that fails
    again is told again)."""
    subject = {"release_id": release.id}
    if release.requested_by_id is None or sent(session, project.org_id, FAILED, subject, release.updated_at):
        return 0
    requester = _member_in(session, release.requested_by_id, project.org_id)
    if requester is None:
        return 0
    action = str((release.failure or {}).get("action") or "retry")
    label = ACTION_LABELS.get(action, ACTION_LABELS["retry"])
    # The tab holds the action (and the PR or run it opens): a notice links only this console's own pages.
    return _send(session, project, [requester], FAILED, f"Release failed: {project.name}. {label}.", _tab_link(project),
                 subject, release.updated_at)


def rolled_back(session: Session, project: Project, release: Release, stage: Optional[ReleaseStage]) -> int:
    """"Rolled back" once a roll back reached its stage: to who pressed it and the requester of the release it undid
    (each once)."""
    subject = {"release_id": release.id}
    if sent(session, project.org_id, ROLLED_BACK, subject, release.requested_at):
        return 0
    undone = session.get(Release, release.rolls_back_id) if release.rolls_back_id is not None else None
    ids = list(dict.fromkeys(i for i in (release.requested_by_id, undone.requested_by_id if undone else None) if i))
    people = [p for p in (_member_in(session, i, project.org_id) for i in ids) if p is not None]
    if not people:
        return 0
    where = stage.name if stage is not None else "The stage"
    text = f"Rolled back: {where} of {project.name} is back on {release.version or 'its previous version'}."
    return _send(session, project, people, ROLLED_BACK, text, _tab_link(project), subject, release.requested_at)


# ── The bell (`routes/inbox.py`'s `release_*` kinds) ─────────────────────────────────────────────────


def wire(row: ReleaseNotice) -> dict[str, str]:
    """The bell's shape: the server's sentence and where it opens (the project's pipeline page)."""
    summary = row.summary or {}
    return {"text": str(summary.get("text") or ""), "href": str(summary.get("href") or tab_path(row.project_id))}


def feed(session: Session, org_id: UUID, user_id: UUID, *, unread_only: bool, limit: int) -> list[ReleaseNotice]:
    """The caller's own release notices in their active org, newest first."""
    query = (
        select(ReleaseNotice)
        .where(ReleaseNotice.org_id == org_id, ReleaseNotice.user_id == user_id)
        .order_by(ReleaseNotice.created_at.desc(), ReleaseNotice.id.desc())
        .limit(limit)
    )
    if unread_only:
        query = query.where(ReleaseNotice.read_at.is_(None))
    return list(session.scalars(query))


def set_read(session: Session, org_id: UUID, user_id: UUID, notice_ids: list[UUID], *, read: bool) -> int:
    """Stamp (or, `read` False, clear) the caller's read of each named notice of their own; anyone else's is
    skipped silently."""
    return trigger_notices.set_read(session, org_id, user_id, notice_ids, read=read, model=ReleaseNotice)
