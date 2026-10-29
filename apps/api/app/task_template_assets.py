"""Task-template images: what an upload may be, and the copy seam that carries them onto a task or a duplicate.

A GLOBAL template's images are `task_template_assets` rows; an org template's are `task_attachments` rows on
`org_template_id`. Either way the bytes are one S3 object, shared by key with every copy, and removed only by
`app/attachment_storage.py::reap_unreferenced_object` once no row of either store names it. The routes are
`app/routes/task_templates.py` (upload, content, delete).

Each copy takes only the assets whose `filename` the body names as `attachment:<filename>`, sharing the `s3_key`
(the pattern of `app/automation_fire.py::_copy_attachments`), so an image is copied exactly once. The task create
seam (`app/routes/tasks/create.py::_link_template`) calls the first, the duplicate route
(`app/routes/task_templates.py::duplicate_template`) the second.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.task_templates import OrgTaskTemplate, TaskTemplate, TaskTemplateAsset

if TYPE_CHECKING:
    from app.models.tasks import Task

CARD_IMAGE, BODY_IMAGE = "card_image", "body_image"
#: A card image's cap: drawn at card size, so 2 MiB is plenty.
MAX_CARD_IMAGE_SIZE = 2 * 1024 * 1024
SVG = "image/svg+xml"
#: What a card image may be. SVG is served only under `SVG_HEADERS`, never by a presigned S3 URL.
CARD_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", SVG)
#: What a body image may be: a pasted screenshot, raster only, as a comment image
#: (`routes/attachments.py::COMMENT_IMAGE_CONTENT_TYPES`) — it is copied onto tasks, whose content route serves
#: it without the SVG headers.
BODY_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
#: How many images one template may hold — a body's screenshots and its card, with room to spare.
MAX_ASSETS_PER_TEMPLATE = 50
#: An SVG response's headers: no script, no fetch, no sniffing it into something else — and, opened on its own,
#: no form or base URL to phish with (those directives do not fall back to `default-src`).
SVG_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; sandbox; form-action 'none'; base-uri 'none'; "
        "frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
}
ASSET_PREFIX = "asset:"
_TOKEN_SCHEME = "attachment:"
_TOKEN_TERMINATORS = (")", " ", "\n")

AnyAsset = Union[TaskTemplateAsset, TaskAttachment]


def sniffed_type(data: bytes) -> Optional[str]:
    """The image type `data` actually is, from its leading bytes — never the uploader's claim — or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    head = data[:4096].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if head.startswith((b"<svg", b"<?xml", b"<!--", b"<!doctype svg")) and b"<svg" in head:
        return SVG
    return None


def referenced(body: str, filename: str) -> bool:
    """Whether `body` names `filename` as an `attachment:` token — anchored on the token's terminator, so `a.png`
    does not match `attachment:a.png.bak` (the console's `taskDuplicate.ts::isInlineToken` rule)."""
    needle = _TOKEN_SCHEME + filename
    at = body.find(needle)
    while at != -1:
        after = at + len(needle)
        if after == len(body) or body[after] in _TOKEN_TERMINATORS:
            return True
        at = body.find(needle, at + 1)
    return False


def asset_id_of(image_key: Optional[str]) -> Optional[str]:
    """The asset id an `asset:<id>` image key names, else None (a built-in image or none)."""
    if image_key and image_key.startswith(ASSET_PREFIX):
        return image_key.removeprefix(ASSET_PREFIX)
    return None


def assets_of(session: Session, *, template_id: Optional[UUID], org_template_id: Optional[UUID]) -> list[AnyAsset]:
    """A template's uploaded (confirmed) images, oldest first: the global store's rows or the org template's."""
    if org_template_id is not None:
        return list(session.scalars(
            select(TaskAttachment)
            .where(TaskAttachment.org_template_id == org_template_id, TaskAttachment.uploaded_at.is_not(None))
            .order_by(TaskAttachment.id)
        ).all())
    if template_id is None:
        return []
    return list(session.scalars(
        select(TaskTemplateAsset)
        .where(TaskTemplateAsset.template_id == template_id, TaskTemplateAsset.uploaded_at.is_not(None))
        .order_by(TaskTemplateAsset.id)
    ).all())


def _copy_onto(source: AnyAsset, **subject: object) -> TaskAttachment:
    """A tenant row sharing `source`'s object. A global image's uploader is the operator's user, never shown to an
    org, so its copy names none."""
    is_global = isinstance(source, TaskTemplateAsset)
    return TaskAttachment(
        kind=ATTACHMENT_KIND_FILE, filename=source.filename, content_type=source.content_type,
        size_bytes=source.size_bytes, s3_key=source.s3_key, s3_bucket=source.s3_bucket,
        etag=None if is_global else source.etag, uploaded_at=source.uploaded_at,
        uploaded_by_id=None if is_global else source.uploaded_by_id,
        **subject,
    )


def copy_template_assets(
    session: Session, *, template_id: UUID | None, org_template_id: UUID | None, task: "Task", body: str
) -> None:
    """Copy onto `task` each asset of the global (`template_id`) or org (`org_template_id`) template whose filename
    `body` names as `attachment:<filename>`. A key the task already holds is not copied again (AC-10). An SVG (a
    card image only) never is: a task's content route serves no SVG headers."""
    sources = [
        asset for asset in assets_of(session, template_id=template_id, org_template_id=org_template_id)
        if asset.s3_key and asset.content_type != SVG and referenced(body or "", asset.filename)
    ]
    if not sources:
        return
    held = set(session.scalars(select(TaskAttachment.s3_key).where(TaskAttachment.task_id == task.id)).all())
    for source in sources:
        if source.s3_key in held:
            continue
        held.add(source.s3_key)
        session.add(_copy_onto(source, org_id=task.org_id, task_id=task.id))


def copy_assets_between_templates(session: Session, *, source_id: UUID, dest_org_template_id: UUID) -> None:
    """Copy the assets of template `source_id` (global or org) onto the org template `dest_org_template_id`: those
    its body names, and its uploaded card image, which the copy then shows too."""
    dest = session.scalars(select(OrgTaskTemplate).where(OrgTaskTemplate.id == dest_org_template_id)).first()
    source: Optional[Union[TaskTemplate, OrgTaskTemplate]] = session.scalars(
        select(OrgTaskTemplate).where(OrgTaskTemplate.id == source_id)
    ).first()
    source_is_org = source is not None
    if not source_is_org:
        source = session.scalars(select(TaskTemplate).where(TaskTemplate.id == source_id)).first()
    if dest is None or source is None:
        return
    card_id = asset_id_of(source.image_key)
    sources = assets_of(
        session, template_id=None if source_is_org else source_id, org_template_id=source_id if source_is_org else None
    )
    for asset in sources:
        is_card = str(asset.id) == card_id
        if not asset.s3_key or not (is_card or referenced(dest.body, asset.filename)):
            continue
        copy = _copy_onto(asset, org_id=dest.org_id, org_template_id=dest.id)
        session.add(copy)
        if is_card:
            session.flush()
            dest.image_key = f"{ASSET_PREFIX}{copy.id}"

