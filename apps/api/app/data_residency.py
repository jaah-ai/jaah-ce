"""An org's data region (decision D-F4): where the org data Jaah creates OUTSIDE the control-plane
database lives — cloud nodes and NodeLink storage it creates, session transcripts, task artifacts and attachments.

THE REGIONS. The operator's `data_regions` knob: a JSON list of `{region, label, transcript_bucket}`, the control
plane's own region (`AWS_REGION`) first. One bucket per region holds that region's transcripts, artifacts and
attachments; the first entry's bucket may be blank, meaning the configured `SESSION_TRANSCRIPT_BUCKET`. An empty
knob is the control-plane region alone. The control-plane database never moves: an org's account, tasks and
settings stay in the control-plane region whatever it picks (`stays_text`).

THE CHOICE. `orgs.data_region`, NULL until the org picks (= the control-plane region). Picking needs the plan
switch `data_residency_enabled`; losing the switch later keeps the stored region (the data already went there).
A change is REFUSED (409, `blockers`) once the org has any cloud connection, NodeLink storage, transcript or
attachment in another region: moving data between regions is the operator's migration, never a silent one.

ENFORCED AT CREATION. An org that picked a region may not create a cloud connection or NodeLink storage in another
one (`refuse_other_region`); a hosted machine Jaah launches for it goes through the operator's connection in its
region (`hosting_provisioning._hosting_connection`). New transcripts and attachments go to its region's bucket
(`bucket_for_org`); every row keeps the bucket it was written to, so an older object stays readable and the
retention sweep deletes from the row's own bucket. An org that never picked is not fenced: NULL is not a promise.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import edition_features
from app.operator_settings import reads as settings_reads
from app.config import get_settings

#: The plan switch (`plan_terms.SWITCHES`).
SWITCH: Final = "data_residency_enabled"
#: The operator knob (`app/operator_settings/catalog_runners.py`).
KNOB: Final = "data_regions"

_REGION_RE = re.compile(r"[a-z]{2}(-[a-z]+)+-\d{1,2}")
# S3's bucket-name rule as the presigner takes it (`aws/presign.py::s3_object_url`, virtual-hosted, so no dots):
# 3–63 lowercase letters, digits and hyphens.
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_LABEL_MAX = 60


@dataclass(frozen=True)
class Region:
    region: str
    label: str
    #: The bucket for this region's objects; "" on the control-plane entry = the configured bucket.
    transcript_bucket: str


def control_plane_region() -> str:
    """The control plane's own region (`AWS_REGION`); "" on a stack with none (local)."""
    return get_settings().aws_region


def _aws_label(code: str) -> str:
    if not edition_features.enabled("aws"):
        return code
    from app.aws.ec2 import REGIONS  # Local: the EC2 module is heavy.

    return dict(REGIONS).get(code, code)


def parse(text: str) -> list[Region]:
    """The knob's text as regions, control-plane region first. Raises `ValueError` with a sentence naming what to
    fix. Blank = `[]`."""
    if not text.strip():
        return []
    try:
        raw = json.loads(text)
    except ValueError:
        raise ValueError("not valid JSON. Enter a list of regions.") from None
    if not isinstance(raw, list):
        raise ValueError("enter a list of regions.")
    out: list[Region] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"entry {i} must have a region, label and bucket.")
        region = str(item.get("region") or "").strip()
        label = str(item.get("label") or "").strip()
        bucket = str(item.get("transcript_bucket") or "").strip()
        if not _REGION_RE.fullmatch(region):
            raise ValueError(f"entry {i}: “{region[:40]}” is not an AWS region code, such as eu-central-1.")
        if any(r.region == region for r in out):
            raise ValueError(f"{region} is listed twice.")
        # A blank label is AWS's own name for the region, as the panel shows it until edited.
        label = label or _aws_label(region)
        if len(label) > _LABEL_MAX:
            raise ValueError(f"{region} needs a label of 1 to {_LABEL_MAX} characters.")
        if bucket and not _BUCKET_RE.fullmatch(bucket):
            raise ValueError(f"{region}: “{bucket[:63]}” is not an S3 bucket name.")
        if not bucket and i > 1:
            raise ValueError(f"{region} needs a bucket.")
        if i > 1 and bucket == configured_bucket():
            raise ValueError(f"{region} needs its own bucket, not the control plane's.")
        out.append(Region(region=region, label=label, transcript_bucket=bucket))
    if any(r.transcript_bucket and sum(o.transcript_bucket == r.transcript_bucket for o in out) > 1 for r in out):
        raise ValueError("each region needs its own bucket.")
    cp = control_plane_region()
    if out and cp and out[0].region != cp:
        raise ValueError(f"the first region must be the control plane's, {cp}.")
    return out


def canonical(text: str) -> str:
    """The knob's stored form: `parse`d and re-serialised (blank stays blank). Raises `ValueError`."""
    regions = parse(text)
    if not regions:
        return ""
    return json.dumps([{"region": r.region, "label": r.label, "transcript_bucket": r.transcript_bucket} for r in regions])


def regions() -> list[Region]:
    """Every region an org may pick, control-plane region first. A stored value that no longer parses (the control
    plane moved region) reads as the control-plane region alone — never a choice the rules refuse."""
    try:
        listed = parse(settings_reads.get_str(KNOB))
    except ValueError:
        listed = []
    cp = control_plane_region()
    if not listed:
        return [Region(region=cp, label=_aws_label(cp), transcript_bucket="")] if cp else []
    return listed


def label(code: Optional[str]) -> str:
    """A region's label as people read it: the operator's, else AWS's name, else the code."""
    if not code:
        return ""
    return next((r.label for r in regions() if r.region == code), None) or _aws_label(code)


def effective_region(org_region: Optional[str]) -> str:
    """The region an org's data goes to: its choice, else the control plane's."""
    return org_region or control_plane_region()


def stays_text() -> str:
    """What never leaves the control-plane region, as the setting page says it."""
    cp = control_plane_region()
    return f"Your account, tasks and settings stay in the control-plane region ({cp})." if cp else (
        "Your account, tasks and settings stay in the control-plane region."
    )


def configured_bucket() -> str:
    return get_settings().session_transcript_bucket


def bucket_for_region(code: str) -> str:
    """The bucket a region's objects go to: its entry's, else (the control-plane region, or one no longer listed)
    the configured bucket."""
    found = next((r for r in regions() if r.region == code), None)
    return (found.transcript_bucket if found is not None else "") or configured_bucket()


def region_of_bucket(bucket: Optional[str]) -> str:
    """The region a bucket lives in: its entry's, else the control plane's (the configured bucket, or a legacy row's
    NULL)."""
    if bucket:
        for r in regions():
            if r.transcript_bucket == bucket:
                return r.region
    return control_plane_region()


def org_region(session: Session, org_id: Optional[UUID]) -> Optional[str]:
    """The org's stored choice, None when it made none (or no org)."""
    if org_id is None:
        return None
    from app.models.orgs import Org

    return session.scalar(select(Org.data_region).where(Org.id == org_id))


def bucket_for_org(session: Session, org_id: Optional[UUID]) -> str:
    """The bucket new objects of `org_id` go to: its region's, else the configured bucket."""
    chosen = org_region(session, org_id)
    return bucket_for_region(chosen) if chosen else configured_bucket()


def wrong_region(org_region_code: str) -> str:
    """The refusal for a resource created outside the org's region: what, and what to do."""
    return f"Your organization's data region is {label(org_region_code)}. Choose that region."


def refuse_other_region(session: Session, org_id: Optional[UUID], region: str) -> None:
    """422 naming the org's region when it picked one and `region` is another. An org that never picked is not
    fenced."""
    chosen = org_region(session, org_id)
    if chosen and region != chosen:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail=wrong_region(chosen))


# ── Changing the region ──────────────────────────────────────────────────────────────────────────────


def _plural(n: int, one: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {one}s"


def blockers(session: Session, org_id: UUID, target: Optional[str]) -> Optional[str]:
    """Why the org may not move to `target`, or None: its data outside `target` (`target` None = anywhere, what
    "locked" means on the page), counted per kind and region, as one sentence that says what to do. Counts what
    Jaah created that has a region: cloud connections, NodeLink storage (its own stores, and its NodeLinks on
    Jaah-managed storage), launched machines (their own connection's or a hosted one's), and every session or file
    with a stored bucket — a session's is fixed at its first object (`session_transcript_archive.pin_bucket`), so an
    unfinished upload or a live stream counts."""
    from app.models.agent_sessions import AgentSession
    from app.models.credentials import CloudConnection
    from app.models.nodelinks import OrgSharedStore
    from app.models.nodes import Node
    from app.models.task_extras import TaskAttachment

    found: dict[str, dict[str, int]] = {}

    def note(region: Optional[str], n: int, what: str) -> None:
        if n and region and region != target:
            kinds = found.setdefault(region, {})
            kinds[what] = kinds.get(what, 0) + n

    for region, n in session.execute(
        select(CloudConnection.region, func.count()).where(CloudConnection.org_id == org_id)
        .group_by(CloudConnection.region)
    ):
        note(region, n, "cloud connection")
    for region, n in session.execute(
        select(OrgSharedStore.region, func.count()).where(OrgSharedStore.org_id == org_id)
        .group_by(OrgSharedStore.region)
    ):
        note(region, n, "NodeLink store")
    # The community edition has no Jaah-managed NodeLink storage.
    if edition_features.enabled("nodelink"):
        from app import nodelinks  # Local: it imports the AWS and fleet modules.

        for region, n in nodelinks.managed_regions(org_id).items():
            note(region, n, "NodeLink")
    node_region = Node.infra["region"].astext
    for region, n in session.execute(
        select(node_region, func.count()).where(Node.org_id == org_id, node_region.is_not(None))
        .group_by(node_region)
    ):
        note(region, n, "machine")
    for bucket, n in session.execute(
        select(AgentSession.transcript_bucket, func.count())
        .where(AgentSession.org_id == org_id, AgentSession.transcript_bucket.is_not(None))
        .group_by(AgentSession.transcript_bucket)
    ):
        note(region_of_bucket(bucket), n, "session")
    for bucket, n in session.execute(
        select(TaskAttachment.s3_bucket, func.count())
        .where(TaskAttachment.org_id == org_id, TaskAttachment.s3_key.is_not(None))
        .group_by(TaskAttachment.s3_bucket)
    ):
        note(region_of_bucket(bucket), n, "file")
    if not found:
        return None
    where = "; ".join(
        f"{label(region)}: {', '.join(_plural(n, what) for what, n in kinds.items())}"
        for region, kinds in sorted(found.items())
    )
    return f"Your organization has data in {where}. Delete it, or ask support to migrate it, then change the region."


def refuse_dropped(session: Session, text: str) -> None:
    """`ValueError` when the knob's new value `text` drops a region, or changes its bucket, while an org has chosen
    that region or a row's objects are in that bucket: their reads, signed for the bucket's region, and the org's
    promise both depend on the entry (`region_of_bucket`). Called by `operator_settings.writes.apply_overrides`."""
    from app.models.agent_sessions import AgentSession
    from app.models.orgs import Org
    from app.models.task_extras import TaskAttachment

    kept = {(r.region, r.transcript_bucket) for r in parse(text)}
    try:
        current = parse(settings_reads.get_str(KNOB))
    except ValueError:
        return
    for old in current:
        if (old.region, old.transcript_bucket) in kept:
            continue
        if session.scalar(select(func.count()).select_from(Org).where(Org.data_region == old.region)):
            raise ValueError(f"organizations keep their data in {old.label}. Keep it listed with its bucket.")
        if old.transcript_bucket and (
            session.scalar(select(func.count()).where(TaskAttachment.s3_bucket == old.transcript_bucket))
            or session.scalar(select(func.count()).where(AgentSession.transcript_bucket == old.transcript_bucket))
        ):
            raise ValueError(f"files are stored in {old.label}'s bucket. Keep it listed with its bucket.")
