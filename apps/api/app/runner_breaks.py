"""Broken sessions: the fleet's record of runners that broke instead of finishing.

A broken session ended without delivering its outcome because the session broke (usage window,
credit, API error, timeout, crash, a runner that vanished and was reclaimed, or a launch never seen running), not because the work
failed. Each is classified by cause (`app/runner_causes.py::CAUSES`, re-exported here) so fixes can be aimed.

An incident is a hidden `adhoc` system Task in `system_failed`, so it inherits the Needs-attention
surface. The `runner-break:<cause>:<task>` dedup key, stamped as a `<!-- sysdedup:… -->` marker,
separates it (`list_incidents` filters on it); one row per (cause, task), refreshed with the newest
evidence. Capture is best-effort: a recording failure is a logged no-op, never a failed settle.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from app.actors import task_actor_values
from app.events import emit_console_event
from app.models.projects import Project
from app.models.tasks import Task
from app.routes.tasks.policy import PERSONA_ADHOC
from app.persona_registry import SYSTEM_PERSONAS
# Re-exported: every reader of `runner_breaks.CAUSES` / `TERMINAL_CAUSES` keeps working. Home: `app/runner_causes.py`.
from app.runner_causes import CAUSES, TERMINAL_CAUSES  # noqa: F401
from app.routes.tasks.create import _snapshot_body_version
from app.routes.tasks.agent_filing import sysdedup_marker
from app.task_lifecycle import AWAITING_RESPONSE, PAUSED, SYSTEM_FAILED

# The dedup-key namespace, stamped into the body as `<!-- sysdedup:KEY -->`; it is the only thing that
# distinguishes a broken-session incident from other hidden `adhoc` rows.
DEDUP_NAMESPACE = "runner-break"

# The body substring `list_incidents` selects on (a string match avoids a new persona or column).
# Derived from `sysdedup_marker` so the selector cannot drift from the stamp.
_MARKER_PREFIX = sysdedup_marker(f"{DEDUP_NAMESPACE}:")[: -len(" -->")]

# `adhoc` is the shared system catch-all; the namespace above keeps this collection separable.
INCIDENT_PERSONA = PERSONA_ADHOC

# Default age for the stale-escalation purge: long enough to spare active investigations, short enough
# to keep the collection about what is breaking now.
DEFAULT_STALE_DAYS = 14

# The states a stale escalation may be purged from; each is a row nobody is acting on. `ready`/`active`
# is live work and never touched.
PURGEABLE_STATES = (SYSTEM_FAILED, PAUSED, AWAITING_RESPONSE)


def dedup_key(cause: str, task_id: int) -> str:
    """The per-source dedup key for one (cause, task) pair; see the module docstring for why the pair."""
    return f"{DEDUP_NAMESPACE}:{cause}:{task_id}"


def incident_title(cause: str, task_id: int, project: str) -> str:
    """The one line an operator reads on the board, led by the cause the collection is counted by."""
    return f"broken session: {cause} — task {task_id} in `{project}`"


UNKNOWN = "unknown"


def predecessor_pane(pane_written_at: Optional[datetime], launch_at: Optional[datetime]) -> str:
    """Whose pane log an incident carries: `yes` when it was last written before this attempt launched (a
    predecessor's — a relaunch into a held slot inherits the session row), `no` when at or after, `unknown`
    when either stamp is missing. By time, not session id: ids are reused across a relaunch."""
    if pane_written_at is None or launch_at is None:
        return UNKNOWN
    return "yes" if pane_written_at < launch_at else "no"


def node_label(name: str, node_id: str, *, own_node: bool) -> str:
    """The incident's `node` field: `name (uuid)` for the task's own org's node, `uuid (shared node)` for a
    shared one (another org's machine, whose name the task's org must not read), `unknown` when neither is
    known."""
    if not node_id:
        return name if own_node and name else UNKNOWN
    if own_node:
        return f"{name} ({node_id})" if name else node_id
    return f"{node_id} (shared node)"


def incident_body(
    cause: str,
    task_id: int,
    *,
    detail: str = "",
    session_id: str = "",
    node_id: str = "",
    account: str = "",
    transcript: str = "",
    observed_by: str = "",
    launch: str = "",
    predecessor_pane: str = "",
    occurrences: int = 1,
) -> str:
    """The incident record. Transcript path and session id make the cause checkable after the fact. Empty
    fields are omitted, so a sparse `vanished` incident still reads cleanly.
    """
    lines = [
        f"Runner session for task {task_id} broke: **{cause}**.",
        "",
        # Scope-neutral: the row lands in the task's org (`app/tenancy/copy.py`). Censused by
        # `tests/test_tenant_system_rows.py`.
        "Nothing was delivered — the session ended before it could record an outcome. This row is the "
        "evidence, not a work item: it is collected so breaks are counted and the cause fixed once, "
        "rather than re-dispatched around forever.",
        "",
    ]
    fields = (
        ("cause", cause),
        ("detail", detail),
        ("task", str(task_id)),
        ("session", session_id),
        ("node", node_id),
        ("launch", launch),
        ("predecessor pane", predecessor_pane),
        ("account", account),
        ("transcript", transcript),
        ("observed by", observed_by),
        # Written even at 1, so a missing field is never the only way to read "once". Prior bodies stay in
        # `task_body_versions`.
        ("occurrences", str(occurrences)),
    )
    lines.extend(f"- **{name}**: {value}" for name, value in fields if value)
    return "\n".join(lines)


_OCCURRENCES_RE = re.compile(r"^- \*\*occurrences\*\*: (\d+)$", re.M)


def occurrences_of(body: str) -> int:
    """How many times this incident's row has been written, read off its own body. 0 when absent or
    unparseable, so a refresh never crashes on the evidence it preserves.
    """
    match = _OCCURRENCES_RE.search(body or "")
    return int(match.group(1)) if match else 0


def find_incident(session: Session, cause: str, task_id: int) -> Optional[Task]:
    """The open incident row for one (cause, task), or None. Matches on the marker (identity), never the
    title (prose).
    """
    marker = sysdedup_marker(dedup_key(cause, task_id))
    return session.scalars(
        select(Task)
        .where(
            Task.persona == INCIDENT_PERSONA,
            Task.body.contains(marker),
            Task.archived_at.is_(None),
        )
        .order_by(Task.id)
    ).first()


def record(
    session: Session,
    *,
    task: Task,
    cause: str,
    detail: str = "",
    session_id: str = "",
    node_id: str = "",
    account: str = "",
    transcript: str = "",
    observed_by: str = "",
    launch: str = "",
    predecessor_pane: str = "",
) -> tuple[int, bool]:
    """Record one broken session. Returns `(incident_id, created)`; `created` False when this refreshed
    the existing row for the same (cause, task).

    Its own upsert, not `create_system_task`: that route excludes terminal rows from dedup, but an
    incident is born terminal, so it would insert a row per break. The marker convention and body-version
    snapshot are shared; the advisory lock is this module's own.

    Inserted directly `system_failed`: it was never dispatched, so `end_attempt` would spend a phantom
    attempt and release a slot nobody holds. The cause goes on `awaiting_response_body`, which the board
    renders for a spent Task.
    """
    key = dedup_key(cause, task.id)
    # Same anchor `create_system_task` takes: the SELECT and INSERT are two statements, and two concurrent
    # observers of one break (the hook and the CP's reclaim) would each insert.
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"runner_break:{key}"))))
    existing = find_incident(session, cause, task.id)
    project = session.get(Project, task.project_id)
    project_name = project.name if project else str(task.project_id)
    # The incident belongs to the broken task's org (`tasks.org_id` is NOT NULL, `app/models/base.py::OrgScoped`).
    incident_org = task.org_id
    body = incident_body(
        cause, task.id,
        detail=detail, session_id=session_id, node_id=node_id, account=account,
        transcript=transcript, observed_by=observed_by, launch=launch, predecessor_pane=predecessor_pane,
        occurrences=(occurrences_of(existing.body) if existing else 0) + 1,
    )
    title = incident_title(cause, task.id, project_name)
    marker = sysdedup_marker(key)
    if existing is not None:
        # Snapshot the outgoing evidence first: the version chain is the per-break history.
        _snapshot_body_version(existing, "runner_break_refresh", session)
        # Repair an unstamped row on refresh, since the insert arm never sees it again. None remain today;
        # kept like the fallback above. Only NULL to an org, never org to a different org, matching
        # `app/tenancy/write_seam.py` (a NULL is absent, a mismatch is wrong).
        if existing.org_id is None and incident_org is not None:
            existing.org_id = incident_org
        existing.title = title
        existing.body = f"{body}\n{marker}"
        existing.awaiting_response_body = _terminal_reason(cause, detail)
        session.commit()
        return existing.id, False
    incident = Task(
        project_id=task.project_id,
        # A node task's incident keeps its pin: a system project holds pinned rows only (`app/node_targets.py`),
        # and an incident is born `system_failed`, so the pin places nothing.
        pinned_node_id=task.pinned_node_id,
        org_id=incident_org,
        title=title,
        body=f"{body}\n{marker}",
        persona=INCIDENT_PERSONA,
        state=SYSTEM_FAILED,
        hidden=True,
        parent_task_id=None,
        awaiting_response_body=_terminal_reason(cause, detail),
    )
    session.add(incident)
    session.flush()   # assign incident.id before the event emit
    emit_console_event(session, entity="task", id=incident.id, action=SYSTEM_FAILED)
    session.commit()
    return incident.id, True


def _terminal_reason(cause: str, detail: str) -> str:
    """The one line the console shows for a spent Task: cause first, evidence after, like the title."""
    return f"broken session: {cause}" + (f" — {detail}" if detail else "")


def _incident_select() -> Select:
    return (
        select(Task, Project.name)
        .join(Project, Project.id == Task.project_id)
        .where(
            Task.persona == INCIDENT_PERSONA,
            Task.body.contains(_MARKER_PREFIX),
            Task.archived_at.is_(None),
        )
    )


def list_incidents(
    session: Session, *, cause: Optional[str] = None, limit: int = 200
) -> list[tuple[Task, str]]:
    """The broken-session collection, newest first. Returns `(Task, project_name)` pairs. Archived
    incidents are excluded so a purged row does not come back.

    `cause` narrows by matching the marker, so the filter cannot disagree with the dedup key.
    """
    query = _incident_select()
    if cause:
        query = query.where(Task.body.contains(f"{_MARKER_PREFIX}{cause}:"))
    return list(session.execute(query.order_by(Task.id.desc()).limit(limit)).all())


def cause_of(task: Task) -> str:
    """The cause carried by an incident row's marker, or `''` for a row that has none. Read off the
    marker (identity), never the body prose.
    """
    if not task.body:
        return ""
    start = task.body.find(_MARKER_PREFIX)
    if start == -1:
        return ""
    rest = task.body[start + len(_MARKER_PREFIX):]
    return rest.split(":", 1)[0]


def stale_escalations(
    session: Session, *, days: int = DEFAULT_STALE_DAYS, states=PURGEABLE_STATES
) -> list[tuple[Task, str]]:
    """Every hidden system escalation row nobody has touched in `days`: the purge's work-list.

    Covers all automated escalations, not just broken sessions, but only rows with a `sysdedup:` marker
    and a `SYSTEM_PERSONAS` persona, so an operator's own Task is never swept.

    Returns `(Task, project_name)` pairs, oldest first; see `archive`.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return list(
        session.execute(
            select(Task, Project.name)
            .join(Project, Project.id == Task.project_id)
            .where(
                Task.persona.in_(SYSTEM_PERSONAS),
                Task.hidden.is_(True),
                Task.body.contains("<!-- sysdedup:"),
                Task.state.in_(tuple(states)),
                Task.archived_at.is_(None),
                Task.updated_at < cutoff,
            )
            .order_by(Task.updated_at)
        ).all()
    )


def archive(session: Session, task_ids: Iterable[int]) -> int:
    """Stamp `archived_at` on the given rows. Archived, not destroyed: every other column is left as found,
    so a wrong-window purge is recoverable by clearing one column. Returns the number stamped;
    already-archived rows are skipped, so a re-run is a no-op.
    """
    ids = list(task_ids)
    if not ids:
        return 0
    result = session.execute(
        update(Task)
        .where(Task.id.in_(ids), Task.archived_at.is_(None))
        .values(archived_at=datetime.now(timezone.utc), **task_actor_values(session))
    )
    return int(result.rowcount or 0)
