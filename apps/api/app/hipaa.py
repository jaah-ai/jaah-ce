"""HIPAA mode: the single home of every HIPAA number and list (architecture K2).

An org is in HIPAA mode when the operator turned it on after a BAA (`Org.hipaa_enabled`, with
`Org.hipaa_baa_effective_date`; only `routes/orgs.py::update_org` writes them). OFF is every org's default
and changes nothing: every HIPAA check in the codebase asks `is_on`/`org_is_on` first.

`SURFACES` names each integration that can carry an org's data to a vendor, mapped to that vendor's id. A
surface is WARNED while its vendor has no BAA on file: the operator lists the vendors that do in the
`hipaa_vendor_baas` setting (`app/operator_settings/`), so the default (empty) warns every surface. An org
admin's acknowledgement of a warning is kept per surface in `Org.hipaa_acks`
(`{surface: {"by": <admin_user uuid>, "at": <iso ts>}}`), written by
`routes/orgs.py::acknowledge_hipaa_warning`.

`record_phi_read` is the PHI access audit (AC5.7): each API read of a PHI-bearing record in a HIPAA org —
a task, its comments, an attachment, an artifact, a session transcript — leaves one `phi_read`
data-access row naming who, which record, when and from where. OFF orgs record nothing new.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app import agent_providers, data_access_events
from app.operator_settings import reads as settings_reads
from app.models.identity import AdminUser
from app.models.orgs import Org

#: A HIPAA org's console session ends after this long with no activity (C5).
IDLE_TIMEOUT = timedelta(minutes=15)
#: How often a HIPAA org's open console refreshes its session while active (C5).
REFRESH_AFTER = timedelta(seconds=60)

#: Surface -> the vendor that receives the org's data through it. An agent CLI's surface is its provider value,
#: its vendor the catalogue row's `capabilities.vendor` (`app/agent_providers.py`); a consumer subscription is
#: its own surface (`agent_providers.SUBSCRIPTION_SURFACES`), since a consumer plan reaches a different vendor
#: entity than an API key does.
SURFACES: dict[str, str] = {
    "slack": "slack",
    "telegram": "telegram",
    **{p.value: p.capabilities.vendor for p in agent_providers.PROVIDERS},
    **agent_providers.SUBSCRIPTION_SURFACES,
    "help_chat": "google",
    "jira": "atlassian",
}

#: What a `phi_read` row's `record_type` may name, each with its summary template.
PHI_RECORD_SUMMARIES: dict[str, str] = {
    "task": "Read task {record_id}",
    "comments": "Read the comments of task {record_id}",
    "attachment": "Read attachment {record_id} of task {task_id}",
    "artifact": "Read artifact {record_id} of task {task_id}",
    "transcript": "Read the transcript of session {record_id}",
    "session_events": "Read the event stream of session {record_id}",
    "pane_log": "Read the pane log of session {record_id}",
}

#: The longest User-Agent a `phi_read` row keeps.
USER_AGENT_MAX = 512

#: The operator setting listing vendor ids with a BAA on file (comma-separated).
VENDOR_BAAS_SETTING = "hipaa_vendor_baas"


def is_on(org: Optional[Org]) -> bool:
    """Whether this org is in HIPAA mode. No org is never on."""
    return org is not None and bool(org.hipaa_enabled)


def org_is_on(session: Session, org_id: Optional[UUID]) -> bool:
    """`is_on` for an org named by id; an unknown or absent id is off."""
    return org_id is not None and is_on(session.get(Org, org_id))


def idled_out(age_seconds: float) -> bool:
    """Whether a HIPAA org's session `age_seconds` past its last refresh has idled out (`IDLE_TIMEOUT`)."""
    return age_seconds > IDLE_TIMEOUT.total_seconds()


def vendors_with_baa() -> frozenset[str]:
    """The vendor ids the operator lists as having a BAA on file."""
    raw = settings_reads.get_str(VENDOR_BAAS_SETTING)
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def warned_surfaces(session: Session) -> dict[str, str]:
    """`SURFACES` whose vendor has no BAA on file, surface -> vendor, in `SURFACES` order.

    `session` is unused today (the vendor list is an operator setting); it is in the signature so a later
    per-org source does not change every caller."""
    del session
    covered = vendors_with_baa()
    return {surface: vendor for surface, vendor in SURFACES.items() if vendor not in covered}


def org_warnings(session: Session, org_id: Optional[UUID]) -> list[tuple[str, bool]]:
    """The warned surfaces of an org in HIPAA mode, each with whether an admin acknowledged it, in `SURFACES`
    order; empty while off. What every member's console reads (`routes/auth.py::AdminOut.hipaa_warnings`)."""
    org = session.get(Org, org_id) if org_id is not None else None
    if not is_on(org):
        return []
    acks = org.hipaa_acks or {}
    return [(surface, surface in acks) for surface in warned_surfaces(session)]


def runner_label(task_id: int) -> str:
    """The `actor_label` of a read an agent made on behalf of this task's runner."""
    return f"runner of task {task_id}"


def stage_phi_read(
    session: Session,
    request: Request,
    *,
    record_type: str,
    record_id: object,
    org_id: Optional[UUID],
    on: Optional[bool] = None,
    task_id: Optional[int] = None,
    user: Optional[AdminUser] = None,
    user_id: Optional[UUID] = None,
    actor_label: Optional[str] = None,
) -> bool:
    """Stage one `phi_read` row for this read on `session`, uncommitted, and say whether it did — nothing when
    the record's org is not in HIPAA mode or a person's request is a background poll (`X-Activity:
    background`, `app/login_funnel.py`). `on` is the org's mode when the caller read it already (a route
    that must issue no SQL after its own commit); otherwise it is looked up.

    The actor is `user`, or `user_id`/`actor_label` read off it earlier; an agent read names its runner in
    `actor_label` alone."""
    if record_type not in PHI_RECORD_SUMMARIES:
        raise ValueError(f"{record_type!r} is not a PHI record type")
    # At call time: `login_funnel` imports `mfa`, which imports this module.
    from app.login_funnel import ACTIVITY_HEADER, source_ip

    # Only a person's console marks a poll as background; an agent read is recorded whatever it sends.
    by_person = user is not None or user_id is not None
    if by_person and request.headers.get(ACTIVITY_HEADER) == "background":
        return False
    if not (org_is_on(session, org_id) if on is None else on):
        return False
    user_agent = request.headers.get("user-agent")
    data_access_events.record(
        session,
        data_access_events.PHI_READ,
        PHI_RECORD_SUMMARIES[record_type].format(record_id=record_id, task_id=task_id),
        data={
            "record_type": record_type,
            "record_id": str(record_id),
            "task_id": task_id,
            "ip": source_ip(request),
            # Caller-supplied, so bounded: the row is kept for years.
            "user_agent": user_agent[:USER_AGENT_MAX] if user_agent else None,
        },
        org_id=org_id,
        user=user,
        user_id=user_id,
        actor_label=actor_label,
    )
    return True


def record_phi_read(session: Session, request: Request, **fields: object) -> None:
    """`stage_phi_read`, then COMMIT what it staged. The one write the cookie-authenticated task-detail and
    comment GETs make (`tests/test_git_access.py` exempts exactly this call)."""
    if stage_phi_read(session, request, **fields):  # type: ignore[arg-type]
        session.commit()
