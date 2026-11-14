"""Task attachment upload + list + delete, and the deliverable-artifact reads.

Metadata lives in `task_attachments`; bytes live in S3 (`app/attachment_storage.py` is the single
home for why and how). `POST …/attachments/ticket` mints a pending row and a presigned PUT, the client
transfers, and `POST …/attachments/{id}/confirm` checks the object landed. The multipart routes upload
from this process (`_store_whole_file`) for callers already holding a bounded file, and refuse with
the ticket door when no bucket resolves. Runner materialization is `apps/jaahd/fleet_dispatch.py`
(via `TaskDispatchOut.attachments` and the agent content route); `user_task_attachment_content` is the
browser read path.

Direction is the axis: `attachment` (`ATTACHMENT_KIND_FILE`) is a human's dispatch input;
`comment_image` (`ATTACHMENT_KIND_COMMENT_IMAGE`) is a human's image owned by one comment — minted by
`…/comment-images/ticket`, linked by `app/comment_service.py`, reaped by `app/routes/task_comments.py`, served only by
`user_task_attachment_content`, listed and materialized nowhere; every other kind
(`app/models/task_extras.py::ATTACHMENT_KINDS`, from `app/task_manifest.py::DOCUMENT_KINDS`) is a runner
deliverable — `report`, `prd`, `architecture`, and free-form `file` (not to be confused with the input
constant). `GET /{task_id}/artifacts` lists them, `…/content` serves one (disposition decided in
`artifact_content`), and `agent_upload_artifact` is the runner's upload. Artifacts are not
materialized into a workdir (`app/routes/tasks/task_wire.py::_task_attachments_meta`).
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os.path
import re
from datetime import datetime
from collections.abc import Sequence
from typing import Annotated, Any, Callable, NoReturn, Optional, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app import attachment_storage, data_access_events, data_residency, hipaa, org_quotas, session_transcript_archive
from app.artifact_pinch_zoom import PINCH_ZOOM_SCRIPT, UTF16_BOMS
from app.auth import adopt_user, require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature, infra
from app.clock import utcnow
from app.db_async import DbDep, on_session, release
from app.entity_shape import commit_row, get_or_404, or_404, refused_as
from app.models.identity import AdminUser
from app.models.task_extras import ATTACHMENT_KIND_COMMENT_IMAGE, ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task
from app.routes.agent.auth import _check_token_for_task
from app.routes.agent.task_reads import _content_disposition
# `routes/tasks/` is the persona vocabulary's home; it imports nothing from here, so no cycle.
from app.routes.tasks.policy import PERSONA_EPIC
from app.user_activity import record_task_note
from app.task_ancestry import executor_user_id
from app.task_artifacts import (
    current_artifact_ids,
    uploaded_clause,
    visible_artifact_clause,
    visible_document_artifacts,
)
from app.task_lookup import get_task_or_404
from app.task_manifest import DOCUMENT_KINDS, FREEFORM_KINDS, KIND_MEDIA

router = APIRouter(prefix="/api/tasks", tags=["attachments"])
# The agent-token half lives here, not in `routes/agent/`: this module imports `routes/agent/task_reads.py`,
# so importing `_read_bounded`/`MAX_ATTACHMENT_SIZE` from here into that package would close an import cycle. `PUBLIC_PREFIXES` covers `/api/agent/`.
agent_router = APIRouter(prefix="/api/agent/tasks", tags=["attachments"])
log = logging.getLogger("cockpit-attachments")

# The ceiling on what may be stored (ticket declarations and `/confirm`'s S3 re-read): S3's single-PUT limit,
# since the bytes stopped traversing this process (D9). Rejected with 413, never truncated. Each org's own
# limit sits under it (`app/org_quotas.py::file_cap_bytes`, which mirrors it as `MAX_FILE_BYTES` — a test pins
# the pair); the buffered doors are held at `MAX_BUFFERED_ATTACHMENT_SIZE` too.
MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024 * 1024

# The whole-file doors' own ceiling: `upload_attachment` and `agent_upload_artifact` still buffer the
# body in the single asyncio process, where a large file would stall the node bus, PTYs and heartbeats. Not a
# leftover to raise — a file this large should use the ticket. `_read_bounded` takes the `min()` with
# `MAX_ATTACHMENT_SIZE`, mirroring `apps/jaahd/cred_broker.py`'s split.
MAX_BUFFERED_ATTACHMENT_SIZE = 25 * 1024 * 1024

# A comment image's own ceiling, refused at its ticket: a pasted screenshot, not a file.
MAX_COMMENT_IMAGE_SIZE = 10 * 1024 * 1024

#: The types a comment image may claim — raster only: the console draws it in an `<img>` on its own
#: origin, and scriptable `image/svg+xml` is safe only under `artifact_content`'s sandbox CSP.
COMMENT_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

#: RFC 9110 `token "/" token` once parameters are stripped. `AttachmentTicketIn.content_type` reaches
#: a signed query parameter (`presign_download`'s `response-content-type`), and `app/aws/presign.py`'s
#: canonical-form gate lets `%0D%0A` through, so this is the allowlist.
MEDIA_TYPE_PATTERN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+/[!#$%&'*+\-.^_`|~0-9A-Za-z]+"

#: The most a ticket request body may declare — far above its three scalars; bounded because
#: `_artifact_ticket_body` reads the whole body in-process and agent tokens reach it.
TICKET_BODY_MAX_BYTES = 8 * 1024

# Filename cap, in UTF-8 bytes: NAME_MAX on the operator's filesystem is bytes, and
# `_content_disposition` spells the name twice (once percent-encoded), so an oversized name yields a
# header a proxy 502s. Refused at upload, where the runner can read the reason.
MAX_FILENAME_LENGTH = 255

# Stripped from display names: C0/C1 controls, zero-width, line/paragraph separators (U+2028/2029 — a
# filename is named in a runner's answer text, where one would break the line) and bidi overrides. The bidi half matters:
# `x<U+202E>exe.pdf` renders as a PDF while being an executable, and `filename*=UTF-8''` carries it
# faithfully, so the header builder cannot handle it. The free-form kind accepts any extension by
# design, so the name must not lie about which it is.
_UNSAFE_NAME_CHARS = re.compile(
    "[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2066-\u2069]"
)

#: The built-in suffix table only (`filenames=()`), so the stored `content_type` does not depend on the
#: image's `/etc/mime.types`. The table still grows between CPython releases, which is why every
#: promised format is pinned in `_EXTRA_SUFFIX_TYPES`.
_BUILTIN_MIME = mimetypes.MimeTypes(filenames=())

#: Promised formats an interpreter may not know: CPython 3.12 (the floor) lacks the OOXML types, and
#: `.md`/`.markdown`/`.webp` vary even between 3.12 patch releases. Pinned so a promise the UI labels
#: (`components/tasks/TaskArtifacts.tsx::CONTENT_TYPE_NAMES`, `artifactKinds.ts`) is decided here.
_EXTRA_SUFFIX_TYPES: dict[str, str] = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".webp": "image/webp",
}

#: Audio and video a browser plays with its own `<audio>`/`<video>`, no plugin: `{suffix: type}`. A literal table,
#: so the stored and served type never depends on the interpreter's `mimetypes` (`_derived_content_type` reads it
#: before that table). Media is never proxied: `media_redirect` hands it to storage by a signed `inline` URL, which answers Range
#: requests, so a file of any size streams and seeks. Inert where it lands: a media element only decodes, the
#: signed type is a media type, and S3's origin is not the console's. A codec the browser lacks shows the player's
#: own error beside Download. Projected to the console by the inline-preview generator.
MEDIA_SUFFIX_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
}

#: Stored types a free-form `file` may be served `inline` as — a closed allowlist (`artifact_content`
#: decides); everything else downloads. `text/html` and scriptable `image/svg+xml` are safe only under
#: the response CSP (`sandbox` without `allow-same-origin`, `default-src 'none'`; drop it and SVG is stored XSS); images and text are
#: inert (`nosniff`); `application/pdf` is inert on the format (see the `pdf` branch of
#: `apps/web/src/components/tasks/TaskArtifacts.tsx`). Keyed on the stored type; each member's round
#: trip is pinned by `test_attachments_routes.py`.
INLINE_FILE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "application/pdf",
        "text/markdown",
        "text/plain",
        "text/csv",
        "application/json",
    }
)


def _stored_filename(raw: Optional[str], fallback: str = "") -> str:
    """The one filename normalisation every upload route here runs.

    The control plane is the trust boundary (agent routes accept any token holder's multipart body).
    One helper, since the hazards belong to the name and its shared header:

      `os.path.basename` — the name reaches no filesystem or key (`attachment_storage.object_key`
        excludes it); this keeps the suggested save name sane. POSIX separators only.
      the control/bidi strip — see `_UNSAFE_NAME_CHARS`.
      `.strip()` — a trailing space would derive octet-stream; also collapses whitespace-only to "".
      the byte cap — see `MAX_FILENAME_LENGTH`, applied to every kind.

    `fallback` applies only to an empty result (e.g. `report.html`); routes that must refuse pass none."""
    name = _UNSAFE_NAME_CHARS.sub("", os.path.basename(raw or "")).strip() or fallback
    if len(name.encode("utf-8")) > MAX_FILENAME_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"a filename may be at most {MAX_FILENAME_LENGTH} bytes — got "
                f"{len(name.encode('utf-8'))}"
            ),
        )
    return name


def _derived_content_type(filename: str) -> str:
    """The stored type for a free-form artifact, derived from the name and never from the multipart
    part (a runner-chosen type decides how the console's origin serves bytes).

    Only the suffix is looked up: `guess_type` honours URL schemes, so `data:,report.pdf` would answer
    `text/plain`. (`guess_file_type` is 3.13+.) An encoded suffix (`.svgz`, `.tgz`) or an unmapped one
    falls to octet-stream rather than naming bytes inside an undeclared wrapper."""
    suffix = os.path.splitext(filename)[1].lower()
    if suffix in _EXTRA_SUFFIX_TYPES:
        return _EXTRA_SUFFIX_TYPES[suffix]
    if suffix in MEDIA_SUFFIX_TYPES:
        return MEDIA_SUFFIX_TYPES[suffix]
    guessed, encoding = _BUILTIN_MIME.guess_type("f" + suffix)
    if guessed is None or encoding is not None:
        return "application/octet-stream"
    return guessed


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    filename: str
    content_type: Optional[str]
    size_bytes: int
    created_at: datetime
    # The task this row was attached FROM — a filer's own artifact (`routes/tasks/agent_filing.py::_attach_caller_artifacts`)
    # or a child's report copied onto its epic. The console shows it as the row's origin.
    source_task_id: Optional[int] = None
    # The data region the bytes are stored in, from the bucket the row was written to
    # (`stored_region`). None for a key-less row, or on a control plane with no region (a local stack).
    region: Optional[str] = None
    region_label: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _with_region(cls, data: Any) -> Any:
        """`model_validate(row)` fills the region from the row; an explicit construction passes `stored_region`."""
        if not isinstance(data, TaskAttachment):
            return data
        fields = {name: getattr(data, name) for name in cls.model_fields if hasattr(data, name)}
        return {**fields, **stored_region(data)}


def stored_region(row: TaskAttachment) -> dict[str, Optional[str]]:
    """`AttachmentOut`'s `region`/`region_label` for `row`: its bucket's region (`data_residency.region_of_bucket`),
    both None for a row with no bytes or on a control plane with no region of its own."""
    if not row.s3_key or not data_residency.control_plane_region():
        return {"region": None, "region_label": None}
    region = data_residency.region_of_bucket(row.s3_bucket)
    return {"region": region, "region_label": data_residency.label(region)}


class ArtifactOut(AttachmentOut):
    """One row of the Artifacts tab: `AttachmentOut` plus whether it is the live row of its kind.

    `current` is derived at read time (latest wins, `app/task_artifacts.py::current_artifact`), agreeing
    by construction with the row the settle's manifest check consulted. For free-form `file` rows it
    means only "newest": many coexist, all are listed and downloadable, and nothing server-side acts on
    the flag for them."""

    current: bool


class AttachmentTicketIn(BaseModel):
    """What a browser declares before it uploads. Name and type are claims: the name is normalised by
    `_stored_filename`.

    `size_bytes` is binding: signed into the presigned PUT (`attachment_storage.presign_upload`), so
    the bucket refuses any other length — otherwise a presigned PUT is unbounded and replayable. Still
    re-read from S3 at `confirm_attachment`, the only evidence the object arrived. `ge=0` lets pydantic
    word the 422.

    `content_type` is stored as claimed and signed into `response-content-type` on redirected reads, so
    it is length-bounded (a huge signed parameter yields a `Location` header proxies reject) and
    shape-checked against `MEDIA_TYPE_PATTERN` (the presign gate is canonical-form only). Parameters are
    dropped; nothing on this path stores them."""

    filename: str
    content_type: Optional[str] = Field(default=None, max_length=255)
    size_bytes: int = Field(ge=0)

    @field_validator("content_type")
    @classmethod
    def _base_media_type(cls, value: Optional[str]) -> Optional[str]:
        """Keep the `type/subtype` and drop any parameters, then require a media type.

        Strip rather than refuse: `text/plain; charset=utf-8` is legitimate and no read uses the
        parameter. Refuse after the strip: the remainder is signed into `response-content-type`."""
        if value is None:
            return None
        base = value.split(";", 1)[0].strip()
        if not base:
            return None
        if not re.fullmatch(MEDIA_TYPE_PATTERN, base):
            raise ValueError(
                "content_type must be an RFC 9110 media type (`type/subtype`), optionally with "
                "parameters that are dropped"
            )
        return base


class ArtifactTicketIn(AttachmentTicketIn):
    """The runner's half of the same declaration, plus `kind`. `content_type` is inherited but never
    read: an artifact's stored type is forced or derived (`_validated_artifact`)."""

    kind: str


class UploadTicketOut(BaseModel):
    """A pending row's id and the one thing a client may do with it: PUT the bytes at `upload_url`, then
    call the matching `/confirm`.

    `upload_url` is a bearer token: this response body is the only place it appears — never logged,
    stored or put in an exception."""

    attachment_id: int
    upload_url: str
    expires_at: datetime


_READ_CHUNK_SIZE = 1024 * 1024


def _refuse_if_epic(task: Task, task_id: int) -> None:
    """An epic is refused on every agent upload route.

    An epic's one artifact is a server-side copy of a child's (`routes/tasks/epic_finish.py::_publish_epic_final_report`),
    which would silently replace a direct upload. Unreachable today (jaahd substitutes the caller's
    own task id), asserted anyway."""
    if task.persona == PERSONA_EPIC:
        raise HTTPException(
            status_code=400,
            detail=f"task {task_id} is an epic — its report is published by the control plane",
        )


def _refuse_oversize(total: int, cap: Optional[int] = None, name: Optional[str] = None) -> None:
    """The 413, and the one place its wording lives (five write paths reach a cap, and the console shows
    `detail` verbatim — `apps/web/src/components/tasks/TaskAttachmentsField.tsx`).

    `cap` is a parameter so the buffered doors can name the lower bound they applied. `None` rather than
    a default of `MAX_ATTACHMENT_SIZE`, which would freeze the value at import and defeat the tests that
    monkeypatch the constant. `name` is the file's, when known with its whole size (a ticket's declaration)."""
    if cap is None:
        cap = MAX_ATTACHMENT_SIZE
    if total > cap:
        limit = org_quotas.size_text(cap)
        raise HTTPException(
            status_code=413,
            detail=(
                f"{name} ({org_quotas.size_text(total)}) is over the {limit} limit." if name
                else f"This file is over the {limit} limit."
            ),
        )


def _refuse_over_quota(session: Session, task_id: int, adding: int) -> None:
    """The per-task and per-org byte quotas (`app/org_quotas.py`, 409), asked by every door that creates a
    task's row from a caller's bytes — the tickets in `_mint_ticket`, the buffered uploads in
    `_store_whole_file` — before the row is written. Support-ticket and support-chat files are not counted."""
    # Every caller has already 404'd an invisible task; a missing org must never read as exempt.
    org_id = or_404(session.scalar(select(Task.org_id).where(Task.id == task_id)), detail="task not found")
    org_quotas.require_attachment_room(session, task_id=task_id, org_id=org_id, adding=adding)


def _task_file_cap(session: Session, task_id: int) -> int:
    """The largest file `task_id`'s org may store (`org_quotas.file_cap_bytes`)."""
    return org_quotas.file_cap_bytes(session, session.scalar(select(Task.org_id).where(Task.id == task_id)))


def _read_bounded(fileobj, cap: Optional[int] = None) -> bytes:
    """Read `fileobj` in bounded chunks, rejecting as soon as the total crosses the buffered cap (a bare
    `.read()` would buffer the whole body before any size check).

    Bounded on the least of `MAX_ATTACHMENT_SIZE`, `cap` (the org's file size, `org_quotas.file_cap_bytes`) and
    `MAX_BUFFERED_ATTACHMENT_SIZE`, so lowering either stored cap lowers this door too (as
    `apps/jaahd/cred_broker.py::_read_confined_upload`)."""
    cap = min(MAX_ATTACHMENT_SIZE, MAX_ATTACHMENT_SIZE if cap is None else cap, MAX_BUFFERED_ATTACHMENT_SIZE)
    chunks: list[bytes] = []
    total = 0
    while chunk := fileobj.read(_READ_CHUNK_SIZE):
        total += len(chunk)
        _refuse_oversize(total, cap)
        chunks.append(chunk)
    return b"".join(chunks)


def _storage_or_503(
    subject_id: int, *, subject: str = "task", bucket: Optional[str] = None
) -> attachment_storage.Target:
    """The bucket every write door needs, or a 503. `bucket` is where the row's bytes go
    (`session_transcript_archive.org_bucket`) or already went (`s3_bucket`); omitted, the question is only "is storage
    configured?", asked before a door buffers or writes anything.

    503, not 500: unconfigured is a shipped state (`attachment_storage.target()` is the single home for
    its causes). Since migration 0251 dropped the `content` column there is no fallback door, and reads
    fail too (`_storage_or_502`): attachments are unavailable in both directions."""
    where = attachment_storage.target(bucket)
    if where is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"attachment storage is not configured on this control plane, so {subject} {subject_id} cannot "
                f"accept an upload — there is no store behind this route to put the bytes in"
            ),
        )
    return where


def _storage_or_502(row: TaskAttachment) -> attachment_storage.Target:
    """The bucket to read a row's bytes from — the one it was written to (`s3_bucket`) — or a 502, the read-side
    twin of `_storage_or_503`.

    502, not 503 or 404: the operator already has this file, and a row with an `s3_key` on a plane with
    no bucket is a misconfiguration upstream, not a missing file."""
    where = attachment_storage.target(row.s3_bucket)
    if where is None:
        raise HTTPException(
            status_code=502,
            detail=(
                f"attachment {row.id} has its bytes in object storage, but this control plane has no "
                f"bucket or credentials configured to read them"
            ),
        )
    return where


def _store_whole_file(
    session: Session,
    attachment: TaskAttachment,
    body: bytes,
) -> None:
    """Put `body` in the bucket and commit `attachment` pointing at it — the whole-file doors' shared
    tail, and the one place here that uploads from inside the control plane.

    The object key derives from the row's own ids (`attachment_storage.object_key`), so the row is
    flushed first; both ids are read off `attachment`, never passed, so the key's prefix has one source. The
    bucket is the org's data region's (`session_transcript_archive.org_bucket`), its org read off the flushed row (the
    write seam stamps it from the subject), and recorded beside the key.

    No transaction is held across the PUT (it would pin a pooled connection for the transfer and starve
    the pool), so the row is committed pending first — `uploaded_at` NULL, invisible to every reader
    (`task_artifacts.uploaded_clause`), exactly like a minted ticket — and stamped after the PUT. A
    failed PUT leaves that pending row behind, unread.

    Composed of its three steps, which an async route awaits apart (the PUT on a worker thread):
    `_stage_whole_file`, `_put_whole_file`, `_stamp_whole_file`."""
    where, key, subject_id = _stage_whole_file(session, attachment, body)
    etag = _put_whole_file(where, key, body, subject_id)
    _stamp_whole_file(session, attachment, etag)


def _stage_whole_file(
    session: Session, attachment: TaskAttachment, body: bytes
) -> tuple[attachment_storage.Target, str, int]:
    """`_store_whole_file`'s first step: the quota asked, the row committed pending with its key and bucket.
    Returns the target, the key, and the subject id the PUT's refusal names."""
    if attachment.automation_id is not None:
        # An automation's Task-action file (`routes/automations.py`): the same caps, counted on the automation.
        org_quotas.require_attachment_room(
            session, task_id=None, org_id=attachment.org_id, adding=len(body), automation_id=attachment.automation_id
        )
    else:
        _refuse_over_quota(session, attachment.task_id, len(body))
    session.add(attachment)
    session.flush()
    # Both ids off the row, after the flush.
    task_id = attachment.task_id if attachment.automation_id is None else attachment.automation_id
    key = (
        attachment_storage.object_key(task_id=attachment.task_id, attachment_id=attachment.id)
        if attachment.automation_id is None
        else attachment_storage.object_key(automation_id=attachment.automation_id, attachment_id=attachment.id)
    )
    where = _storage_or_503(
        task_id, subject="task" if attachment.automation_id is None else "automation",
        bucket=session_transcript_archive.org_bucket(session, attachment.org_id),
    )
    attachment.s3_key = key
    attachment.s3_bucket = where.bucket
    session.commit()
    return where, key, task_id


def _put_whole_file(where: attachment_storage.Target, key: str, body: bytes, task_id: int) -> str:
    """`_store_whole_file`'s PUT (network, no transaction held): the object's ETag, or the 502 that leaves the
    pending row unconfirmed."""
    try:
        return attachment_storage.put(where, key, body)
    except attachment_storage.StorageUnavailable as exc:
        # The type, never the message: `detail` is relayed verbatim to the client.
        raise HTTPException(
            status_code=502,
            detail=(
                f"attachment storage could not accept the upload for task {task_id} "
                f"({type(exc).__name__}) — the row is left unconfirmed and shows nowhere, so the "
                f"upload can simply be retried"
            ),
        ) from None


def _stamp_whole_file(session: Session, attachment: TaskAttachment, etag: str) -> None:
    """`_store_whole_file`'s last step, after the PUT returned: the row stops being pending."""
    attachment.uploaded_at = utcnow()
    attachment.etag = etag or None
    commit_row(session, attachment)


def _mint_ticket(
    session: Session,
    *,
    task_id: Optional[int] = None,
    ticket_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    visibility: Optional[str] = None,
    kind: Optional[str],
    filename: str,
    content_type: Optional[str],
    size_bytes: int,
    uploaded_by_id: Optional[UUID],
    for_browser: bool,
    org_id: Optional[UUID] = None,
) -> UploadTicketOut:
    """Create the PENDING row and sign the one PUT that fills it — the shared half of both ticket routes.

    Flush-then-key-then-commit: the key derives from the row's own id (`attachment_storage.object_key`).
    `uploaded_at` stays NULL, so the row is invisible to every list, read and settle manifest
    (`task_artifacts.uploaded_clause`) until `/confirm` sees the object.

    `for_browser` picks the same-origin spelling of the same signed URL, which only differs on a
    self-hosted `S3_ENDPOINT_URL` (`attachment_storage.BROWSER_ENDPOINT_PATH`).

    The row's subject is `task_id`, a support ticket's `ticket_id` (`routes/support_comments.py`), whose
    file may be minted `internal`, or a support chat's `conversation_id` (`routes/support_chat.py`); the
    key follows the subject (`attachment_storage.object_key`). `org_id` is a ticket's or chat's org, for its
    file size (`org_quotas.file_cap_bytes`); a task's is read off the task."""
    _refuse_oversize(size_bytes)
    # Asked first, so a bucketless plane writes no row; the bucket itself waits for the row's org (below).
    if ticket_id is not None:
        _storage_or_503(ticket_id, subject="ticket")
        _refuse_oversize(size_bytes, org_quotas.file_cap_bytes(session, org_id), filename)
    elif conversation_id is not None:
        _storage_or_503(conversation_id, subject="conversation")
        _refuse_oversize(size_bytes, org_quotas.file_cap_bytes(session, org_id), filename)
    else:
        assert task_id is not None
        _storage_or_503(task_id)
        _refuse_oversize(size_bytes, _task_file_cap(session, task_id), filename)
        _refuse_over_quota(session, task_id, size_bytes)
    attachment = TaskAttachment(
        task_id=task_id,
        ticket_id=ticket_id,
        conversation_id=conversation_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        uploaded_at=None,
        uploaded_by_id=uploaded_by_id,
    )
    if kind is not None:
        # Unset for the human's own file: the column's `server_default` names it.
        attachment.kind = kind
    if visibility is not None:
        attachment.visibility = visibility
    session.add(attachment)
    session.flush()
    key = attachment_storage.object_key(
        task_id=task_id, ticket_id=ticket_id, conversation_id=conversation_id, attachment_id=attachment.id
    )
    # The org's data region's bucket; the org off the flushed row, which the write seam stamped from its subject.
    where = _storage_or_503(
        # Exactly one subject id is set (the route that called this named it), so the `or` chain is never None.
        cast(int, ticket_id or conversation_id or task_id), subject=(
            "ticket" if ticket_id is not None else "conversation" if conversation_id is not None else "task"
        ), bucket=session_transcript_archive.org_bucket(session, attachment.org_id),
    )
    attachment.s3_key = key
    attachment.s3_bucket = where.bucket
    attachment_id = attachment.id
    session.commit()
        # The declared size is signed into the ticket, which makes the 413 above a real cap
        # (`attachment_storage.presign_upload`): a lying client gets a signature its body cannot match.
    upload_url, expires_at = attachment_storage.presign_upload(
        where, key, content_length=size_bytes
    )
    return UploadTicketOut(
        attachment_id=attachment_id,
        upload_url=(
            attachment_storage.browser_url(where, upload_url) if for_browser else upload_url
        ),
        expires_at=expires_at,
    )


async def _complete_pending(
    db: DbDep,
    *,
    task_id: int,
    attachment_id: int,
    kinds: tuple[str, ...],
    cap: Optional[int] = None,
) -> "AttachmentOut":
    """Confirm the object a ticket was minted for actually landed, and make the row visible — the shared
    half of both `/confirm` routes.

    The HEAD is the only evidence the upload happened. A missing object deletes the pending row (no
    reaper otherwise). The size comes from S3, not the ticket's claim (it drives `MAX_PROXY_BYTES` in
    `artifact_content`).

    The cap binds at the ticket (signed length) and is re-asked here as defence in depth; an over-cap
    object is deleted with its row. The object goes first and the key is read before either, or it
    leaks. `reap_unreferenced_object`'s shared-key guard does not apply: a never-completed row cannot
    be an epic's copy.

    409 for a row that is not pending (a second `/confirm`); the keyless arm is a shape check no client
    can reach since 0251. `kinds` is the input/output boundary, as in `delete_attachment`. `cap` is the
    route's own ceiling (a comment image's is lower); `None` is `MAX_ATTACHMENT_SIZE`, read at call time
    for the reason `_refuse_oversize` gives."""
    if cap is None:
        cap = MAX_ATTACHMENT_SIZE
    attachment, where = await db.run_sync(_pending_target, task_id, attachment_id, kinds)
    # Every step so far was a read: its connection goes back before the HEAD.
    await release(db)
    landed = await asyncio.to_thread(_head_or_502, where, attachment.s3_key or "")
    if landed is None:
        await db.run_sync(_drop_unlanded, attachment)
    assert landed is not None
    if landed.size > cap:
        # The object goes first (see above), off the loop.
        await asyncio.to_thread(_drop_over_cap_object, where, attachment.s3_key or "")
        await db.run_sync(_drop_over_cap_row, attachment, landed.size, cap)
    return await db.run_sync(_stamp_landed_out, attachment, landed)


def _pending_target(
    session: Session, task_id: int, attachment_id: int, kinds: tuple[str, ...]
) -> tuple[TaskAttachment, attachment_storage.Target]:
    """`_complete_pending`'s reads, over the route's async session (`run_sync`): the pending row and its bucket."""
    get_task_or_404(task_id, session)
    attachment = get_or_404(session, TaskAttachment, attachment_id, detail="attachment not found", where=(
        TaskAttachment.task_id == task_id, TaskAttachment.kind.in_(kinds)))
    return attachment, _landed_target(attachment)


def _stamp_landed_out(
    session: Session, attachment: TaskAttachment, landed: attachment_storage.Landed
) -> "AttachmentOut":
    """`_stamp_landed`, answered as the row's wire shape, over the route's async session (`run_sync`)."""
    _stamp_landed(session, attachment, landed)
    return AttachmentOut.model_validate(attachment)


def _confirm_landed(session: Session, attachment: TaskAttachment, *, cap: Optional[int] = None) -> None:
    """`_complete_pending`'s subject-agnostic half, for a row its caller already authorized — a task's
    here, a ticket's in `routes/support_comments.py`, a support chat's in `routes/support_chat.py`: HEAD the object and stamp the row readable, or
    remove it (see `_complete_pending`, which also says what `cap` is). Composed of the steps `_complete_pending`
    awaits apart."""
    if cap is None:
        cap = MAX_ATTACHMENT_SIZE
    where = _landed_target(attachment)
    landed = _head_or_502(where, attachment.s3_key or "")
    if landed is None:
        _drop_unlanded(session, attachment)
    if landed.size > cap:
        _drop_over_cap_object(where, attachment.s3_key or "")
        _drop_over_cap_row(session, attachment, landed.size, cap)
    _stamp_landed(session, attachment, landed)


def _landed_target(attachment: TaskAttachment) -> attachment_storage.Target:
    """The bucket a pending row's object was minted into, or the 409 for a row that is not pending."""
    attachment_id = attachment.id
    if attachment.uploaded_at is not None or not attachment.s3_key:
        raise HTTPException(
            status_code=409,
            detail=(
                f"attachment {attachment_id} is not awaiting an upload — it has already been "
                f"completed"
            ),
        )
    if attachment.ticket_id is not None:
        return _storage_or_503(attachment.ticket_id, subject="ticket", bucket=attachment.s3_bucket)
    if attachment.conversation_id is not None:
        return _storage_or_503(attachment.conversation_id, subject="conversation", bucket=attachment.s3_bucket)
    assert attachment.task_id is not None
    return _storage_or_503(attachment.task_id, bucket=attachment.s3_bucket)


def _head_or_502(where: attachment_storage.Target, key: str) -> Optional[attachment_storage.Landed]:
    """The HEAD — the only evidence the upload happened (network): what landed, or None."""
    with refused_as(
        502, attachment_storage.StorageUnavailable, prefix="attachment storage could not be reached to confirm the upload: ",
    ):
        return attachment_storage.head(where, key)


def _drop_unlanded(session: Session, attachment: TaskAttachment) -> NoReturn:
    """No object arrived: the pending row goes (no reaper otherwise), and the 400 says to mint again."""
    attachment_id = attachment.id
    session.delete(attachment)
    session.commit()
    raise HTTPException(
        status_code=400,
        detail=(
            f"the upload did not arrive — no object was found for attachment {attachment_id}, so "
            f"the pending row was removed; mint a new ticket and PUT the bytes again"
        ),
    )


def _drop_over_cap_object(where: attachment_storage.Target, stored_ref: str) -> None:
    """An over-cap object's delete (network), before its row's: key read before the row goes (see docstring)."""
    try:
        attachment_storage.delete(where, stored_ref)
    except attachment_storage.StorageUnavailable:
        # Best effort: refusing to drop the row would be strictly worse. Logged for cleanup.
        log.warning(
            "orphaned over-cap stored object %s: its pending row is being removed", stored_ref
        )


def _drop_over_cap_row(session: Session, attachment: TaskAttachment, size: int, cap: int) -> None:
    """An over-cap upload's row goes after its object, and the 413 says why."""
    attachment_id = attachment.id
    session.delete(attachment)
    session.commit()
    log.warning(
        "attachment %s completed at %s bytes, over the %s byte cap — row and object removed",
        attachment_id, size, cap,
    )
    # Same wording as every other 413, via its one home; `size` already failed, so this raises.
    _refuse_oversize(size, cap)


def _stamp_landed(session: Session, attachment: TaskAttachment, landed: attachment_storage.Landed) -> None:
    """The row made readable at the size and ETag S3 reports."""
    attachment.size_bytes = landed.size
    attachment.etag = landed.etag or None  # none sent: unverifiable, so read as drift (`live_cast/drift.py`)
    attachment.uploaded_at = utcnow()
    commit_row(session, attachment)


def _signed_download_redirect(
    row: TaskAttachment, *, for_browser: bool, inline: bool = False, content_type: str | None = None,
    ttl_seconds: int = attachment_storage.DOWNLOAD_TTL_SECONDS,
) -> RedirectResponse:
    """Hand a bulk read to S3 instead of carrying it (operator decision D4/D8).

    307 (method-preserving) with `Cache-Control: no-store` on the redirect: the target carries a
    five-minute signature (`attachment_storage.DOWNLOAD_TTL_SECONDS`). Filename and type are signed into
    the URL, which suffices for a download but not an inline preview — that needs a header S3 cannot
    attach, so it stays proxied (`artifact_content`)."""
    where = _storage_or_502(row)
    url = attachment_storage.presign_download(
        where,
        row.s3_key or "",
        filename=row.filename,
        # `content_type` overrides the stored type: a preview signs the type its name is served as.
        content_type=content_type if content_type is not None else row.content_type or "",
        inline=inline,
        ttl_seconds=ttl_seconds,
    )
    return RedirectResponse(
        url=attachment_storage.browser_url(where, url) if for_browser else url,
        status_code=307,
        headers={"Cache-Control": "no-store"},
    )


def _artifact_download(artifact: TaskAttachment, task: Task) -> dict:
    """The `artifact_downloaded` row's summary and fields, for `data_access_events.record_committed` —
    called inline by each route so the GET-mutation guard sees (and exempts) exactly that write."""
    return {
        "kind": data_access_events.ARTIFACT_DOWNLOADED,
        "summary": f"Downloaded {artifact.kind} {artifact.filename} of task {task.id}",
        "data": {
            "task_id": task.id,
            "artifact_id": artifact.id,
            "kind": artifact.kind,
            "filename": artifact.filename,
        },
        "org_id": task.org_id,
    }


def _validated_artifact(kind: str, raw_filename: Optional[str]) -> tuple[str, str]:
    """The `(filename, content_type)` a document artifact is stored under, or the 422 that refuses it —
    shared by `agent_upload_artifact` and `agent_artifact_ticket` (which must refuse before minting).

    Membership is tested against `DOCUMENT_KINDS`, the set the refusal names. A format-pinned kind
    (`KIND_MEDIA`) must match its extensions and is stored under its forced type; a free-form kind
    (`FREEFORM_KINDS`) accepts any extension, demands a name, and derives its type from it. The caller's
    `content_type` is never honoured (`curl -F` sends octet-stream, which would turn a readable page
    into a download prompt), and the refusal names the wanted extensions for a runner mid-run."""
    if kind not in DOCUMENT_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown artifact kind {kind!r} — this route stores document artifacts, one of "
                f"{', '.join(DOCUMENT_KINDS)}"
            ),
        )
    # Normalised above the split so every reader sees one string (`_stored_filename`).
    filename = _stored_filename(raw_filename)
    if kind in KIND_MEDIA:
        extensions, content_type = KIND_MEDIA[kind]
        if not filename.lower().endswith(extensions):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"a {kind!r} artifact must be {' or '.join(extensions)} — got {filename!r}, which "
                    f"is stored as {content_type}"
                ),
            )
        return filename, content_type
    # Free-form arm: any extension, but a name is required — it is served as a download and the name
    # is all that identifies it.
    if not filename:
        raise HTTPException(
            status_code=422,
            detail=(
                f"a {kind!r} artifact accepts any extension but must arrive WITH a filename — it is "
                f"served as a download, and the name is the only thing that identifies it"
            ),
        )
    # Derived from the name (`_derived_content_type`).
    return filename, _derived_content_type(filename)


async def _artifact_ticket_body(request: Request) -> ArtifactTicketIn:
    """`{kind, filename, size_bytes}` off a JSON body or a form body.

    Both, because existing runner callers already build a form (the multipart twin's shape); FastAPI
    cannot declare both, hence a dependency reading the request. Async dependency, sync handler: body
    I/O on the loop, DB work in the threadpool. The declared Content-Length is bounded before reading
    (agent-token reachable, single process), as `app/user_activity.py::_WriteCapture` does."""
    declared = (request.headers.get("content-length") or "").strip()
    if not declared.isdigit() or int(declared) > TICKET_BODY_MAX_BYTES:
        raise HTTPException(
            status_code=413 if declared.isdigit() else 411,
            detail=(
                f"an artifact ticket body must declare a Content-Length of at most "
                f"{TICKET_BODY_MAX_BYTES} bytes — it carries three scalar fields"
            ),
        )
    media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    try:
        raw = (
            await request.json()
            if media_type == "application/json"
            else dict(await request.form())
        )
    except Exception:
        # Not relayed: Starlette's parse errors quote the runner's payload.
        raise HTTPException(
            status_code=422, detail="body must be JSON or a form carrying kind, filename and size_bytes"
        ) from None
    try:
        return ArtifactTicketIn.model_validate(raw)
    except ValidationError as exc:
        # A string, not `exc.errors()`, whose `ctx` objects are not all JSON-serialisable.
        raise HTTPException(
            status_code=422,
            detail=f"kind, filename and size_bytes are required — {exc.error_count()} field(s) rejected",
        ) from None


@router.post("/{task_id}/attachments", response_model=AttachmentOut, status_code=201)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
async def upload_attachment(
    db: DbDep,
    task_id: int,
    file: UploadFile = File(...),
    user: AdminUser = Depends(require_user),
) -> AttachmentOut:
    """Upload one file/image onto `task_id` (call repeatedly for more; no batch endpoint).

    Read in bounded chunks against `MAX_BUFFERED_ATTACHMENT_SIZE` — the lower cap, since this door
    buffers in-process. The row commits pending, the bytes go to the bucket, a second commit makes it
    readable (`_store_whole_file`). For callers already holding a bounded file; the console uses
    `create_attachment_ticket`. Not a fallback for a bucketless plane: both doors refuse together
    (`_storage_or_503`)."""
    cap = await db.run_sync(_upload_attachment, task_id)
    # A read so far: its connection goes back before the body is buffered (off the loop: a spooled file).
    await release(db)
    content = await asyncio.to_thread(_read_bounded, file.file, cap)
    where, key, subject_id, attachment = await db.run_sync(
        _stage_attachment, task_id, file.filename, file.content_type, content, user
    )
    etag = await asyncio.to_thread(_put_whole_file, where, key, content, subject_id)
    return await db.run_sync(_stamped_out, attachment, etag)


def _upload_attachment(session: Session, task_id: int) -> int:
    """`upload_attachment`'s checks before the body is read, over the route's async session (`run_sync`): the
    task's file cap."""
    get_task_or_404(task_id, session)
    # Asked before reading the body, so a bucketless plane refuses without buffering.
    _storage_or_503(task_id)
    return _task_file_cap(session, task_id)


def _stage_attachment(
    session: Session,
    task_id: int,
    upload_filename: Optional[str],
    content_type: Optional[str],
    content: bytes,
    user: AdminUser,
) -> tuple[attachment_storage.Target, str, int, TaskAttachment]:
    """`upload_attachment`'s pending row, over the route's async session (`run_sync`): `_stage_whole_file`."""
    user = adopt_user(session, user)
    filename = _stored_filename(upload_filename, "attachment")
    attachment = TaskAttachment(
        task_id=task_id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    where, key, subject_id = _stage_whole_file(session, attachment, content)
    return where, key, subject_id, attachment


def _stamped_out(session: Session, attachment: TaskAttachment, etag: str) -> AttachmentOut:
    """`_stamp_whole_file` after the PUT, answered as the row's wire shape, over the route's async session."""
    _stamp_whole_file(session, attachment, etag)
    return AttachmentOut.model_validate(attachment)


@router.post("/{task_id}/attachments/ticket", response_model=UploadTicketOut, status_code=201)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
@on_session
def create_attachment_ticket(
    session: Session,
    task_id: int,
    body: AttachmentTicketIn,
    user: AdminUser = Depends(require_user),
) -> UploadTicketOut:
    """The upload door the console uses: mint a pending row and a presigned PUT; the browser transfers
    to S3 itself (D4). `upload_attachment` is not a fallback for its 503 (same bucket, same refusal).

    Same declaration as `upload_attachment`: attaching is one permission whichever way the bytes travel.
    The cap is refused here on the declared size — an early no before a large transfer; `/confirm`
    overwrites the size with what S3 received."""
    user = adopt_user(session, user)
    get_task_or_404(task_id, session)
    return _mint_ticket(
        session,
        task_id=task_id,
        kind=None,
        filename=_stored_filename(body.filename, "attachment"),
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        uploaded_by_id=user.id,
        for_browser=True,
    )


# `confirm`, not `complete`: `test_authz_registry.py::test_no_dispatch_verb_ever_carries_the_author_draft_widening`
# refuses `author_draft` on any route ending in a run/settle verb such as `complete`, and this route
# carries it. The agent half and the daemon verb (`artifact.upload_commit`) share the spelling.
@router.post(
    "/{task_id}/attachments/{attachment_id}/confirm", response_model=AttachmentOut
)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
async def confirm_attachment(
    db: DbDep,
    task_id: int,
    attachment_id: int,
    _user: AdminUser = Depends(require_user),
) -> AttachmentOut:
    """The second half of `create_attachment_ticket`: confirms the bytes arrived and makes the row
    visible. Same declaration as the ticket.

    `ATTACHMENT_KIND_FILE` only (`delete_attachment`'s boundary): an operator must not settle a runner's
    pending artifact. The rest is `_complete_pending`."""
    return await _complete_pending(
        db,
        task_id=task_id,
        attachment_id=attachment_id,
        kinds=(ATTACHMENT_KIND_FILE,),
    )


@router.delete("/{task_id}/attachments/{attachment_id}", status_code=204)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
async def delete_attachment(
    db: DbDep,
    task_id: int,
    attachment_id: int,
    user: AdminUser = Depends(require_user),
) -> Response:
    """Remove one input attachment — the inverse of `upload_attachment`, with its declaration verbatim
    (detaching before dispatch is the same edit as attaching). The row goes, and its S3 object is reaped
    behind `reap_unreferenced_object`'s guard.

    The `kind` guard is the input/output boundary: a runner's deliverable shares this table, and a
    `tasks:edit` holder must not destroy one via a surface that never listed it (Reset hides
    deliverables; nothing erases them). The `task_id` cross-check matters because authorization is on the
    path's task while the row is fetched by a global id; compared after the load so a cross-task id gets
    the same 404 as a missing one."""
    stored_ref, stored_bucket = await db.run_sync(_delete_attachment, task_id, attachment_id, user)
    if stored_ref:
        # After the commit, off the loop: the reaper reads on a fleet session of its own and deletes from S3 (its
        # `session` argument is unused).
        await asyncio.to_thread(
            attachment_storage.reap_unreferenced_object, None, stored_ref, stored_bucket  # type: ignore[arg-type]
        )
    return Response(status_code=204)


def _delete_attachment(
    session: Session,
    task_id: int,
    attachment_id: int,
    user: AdminUser,
) -> tuple[Optional[str], Optional[str]]:
    """`delete_attachment`'s body, over the route's async session (`run_sync`): committed; returns the stored key
    and bucket to reap."""
    user = adopt_user(session, user)
    get_task_or_404(task_id, session)
    attachment = get_or_404(session, TaskAttachment, attachment_id, detail="attachment not found", where=(
        TaskAttachment.task_id == task_id, TaskAttachment.kind == ATTACHMENT_KIND_FILE))
    # A delete, unlike an upload, owes a durable record: it destroys the only evidence. `record_task_note`
    # (the task's own feed; `task_note:` stays out of the transition stream), staged in the same
    # transaction, so the audit and the delete land or fail together.
    record_task_note(
        session,
        task_id,
        "attachment_deleted",
        f"Removed attachment {attachment.filename}",
        extra={"attachment_id": attachment.id, "filename": attachment.filename},
        user=user,
    )
    # Read before the delete: after commit the instance is expired.
    stored_ref = attachment.s3_key
    stored_bucket = attachment.s3_bucket
    session.delete(attachment)
    session.commit()
    return stored_ref, stored_bucket


@router.post(
    "/{task_id}/comment-images/ticket", response_model=UploadTicketOut, status_code=201
)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
@on_session
def create_comment_image_ticket(
    session: Session,
    task_id: int,
    body: AttachmentTicketIn,
    user: AdminUser = Depends(require_user),
) -> UploadTicketOut:
    """Stage one image for a comment not yet posted: a pending `comment_image` row and a presigned PUT.

    Posting's own declaration (`routes/task_comments.py::create_comment`), without `author_draft`: an
    image exists only to be commented with. Raster types only (`COMMENT_IMAGE_CONTENT_TYPES`), capped at
    `MAX_COMMENT_IMAGE_SIZE`. The row stays unlinked (`comment_id` NULL) until a post or edit claims it; one
    no post or edit claims is reaped with its bytes (`app/comment_service.py::reap_unclaimed_images`)."""
    user = adopt_user(session, user)
    get_task_or_404(task_id, session)
    if body.content_type not in COMMENT_IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"a comment image must be one of {', '.join(sorted(COMMENT_IMAGE_CONTENT_TYPES))}",
        )
    _refuse_oversize(body.size_bytes, MAX_COMMENT_IMAGE_SIZE)
    return _mint_ticket(
        session,
        task_id=task_id,
        kind=ATTACHMENT_KIND_COMMENT_IMAGE,
        filename=_stored_filename(body.filename, "image"),
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        uploaded_by_id=user.id,
        for_browser=True,
    )


@router.post(
    "/{task_id}/comment-images/{attachment_id}/confirm", response_model=AttachmentOut
)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def confirm_comment_image(
    db: DbDep,
    task_id: int,
    attachment_id: int,
    user: AdminUser = Depends(require_user),
) -> AttachmentOut:
    """The second half of `create_comment_image_ticket`. The uploader's own staged image only — anyone
    else's is the same 404 as a missing one. The rest is `_complete_pending`."""
    await db.run_sync(_confirm_comment_image, attachment_id, user)
    return await _complete_pending(
        db,
        task_id=task_id,
        attachment_id=attachment_id,
        kinds=(ATTACHMENT_KIND_COMMENT_IMAGE,),
        cap=MAX_COMMENT_IMAGE_SIZE,
    )


def _confirm_comment_image(session: Session, attachment_id: int, user: AdminUser) -> None:
    """`confirm_comment_image`'s uploader check, over the route's async session (`run_sync`)."""
    user = adopt_user(session, user)
    uploader = session.scalar(
        select(TaskAttachment.uploaded_by_id).where(TaskAttachment.id == attachment_id)
    )
    if uploader is None or uploader != user.id:
        raise HTTPException(status_code=404, detail="attachment not found")


@router.get("/{task_id}/attachments/{attachment_id}/content")
@feature("tasks", "view", object_type="task", object_params=("task_id",))
async def user_task_attachment_content(
    db: DbDep,
    task_id: int,
    attachment_id: int,
    request: Request,
    preview: Annotated[
        bool,
        Query(
            description=(
                "Render a human's attachment in place (the console's viewer) when its name is a previewable "
                "format (`attachment_preview`); otherwise, and for every other kind, the download."
            ),
        ),
    ] = False,
    *,
    user: AdminUser = Depends(require_user),
) -> Response:
    """Browser-session analogue of `routes/agent/task_reads.py`'s `agent_task_attachment_content`, used by the rich
    editor's `attachment:<token>` resolver. Task-scoped 404 guard; never carries bytes — it redirects to
    a presigned GET.

    Enforces the Reset watermark on every kind except the human's own `ATTACHMENT_KIND_FILE` and
    `ATTACHMENT_KIND_COMMENT_IMAGE` (named positively, so new deliverable kinds are hidden by default): a
    Reset abandons the runner's attempt, not the human's input. The one read of a comment image (the
    Comments tab's `<img src>`); a staged one (`comment_id` NULL, no comment yet) is its uploader's alone,
    the same 404 to anyone else."""
    task, attachment, user = await db.run_sync(_user_task_attachment_content, task_id, attachment_id, user)
    if preview and attachment.kind == ATTACHMENT_KIND_FILE:
        # A read so far: its connection goes back before a proxied preview's S3 GET, made off the loop.
        await release(db)
        shown = await asyncio.to_thread(attachment_preview, attachment)
        if shown is not None:
            await db.run_sync(_record_attachment_read, request, task, attachment, user)
            return shown
    return await db.run_sync(_attachment_download, request, task, attachment, user)


def _user_task_attachment_content(
    session: Session,
    task_id: int,
    attachment_id: int,
    user: AdminUser,
) -> tuple[Task, TaskAttachment, AdminUser]:
    """`user_task_attachment_content`'s guards, over the route's async session (`run_sync`): the task, the row, and
    the caller joined to the session."""
    user = adopt_user(session, user)
    task = get_task_or_404(task_id, session)
    # Whole entity: every column is small since 0251 dropped the bytes column.
    attachment = get_or_404(session, TaskAttachment, attachment_id, detail="attachment not found", where=(
        TaskAttachment.task_id == task_id,
        # Pending rows are not rows yet (`task_artifacts.uploaded_clause`); both predicates fail
        # open when omitted.
        uploaded_clause(),
        or_(
            TaskAttachment.kind.in_((ATTACHMENT_KIND_FILE, ATTACHMENT_KIND_COMMENT_IMAGE)),
            visible_artifact_clause(TaskAttachment.created_at, task.artifacts_hidden_before),
        ),
    ))
    if (
        attachment.kind == ATTACHMENT_KIND_COMMENT_IMAGE
        and attachment.comment_id is None
        and attachment.uploaded_by_id != user.id
    ):
        raise HTTPException(status_code=404, detail="attachment not found")
    if not attachment.s3_key:
        # A key-less row names no bytes; answered as 404 rather than dereferencing NULL.
        raise HTTPException(status_code=404, detail="attachment not found")
    return task, attachment, user


def _record_attachment_read(
    session: Session, request: Request, task: Task, attachment: TaskAttachment, user: AdminUser
) -> None:
    """A served attachment is a PHI read in a HIPAA org, over the route's async session (`run_sync`)."""
    hipaa.record_phi_read(
        session, request, record_type="attachment", record_id=attachment.id, task_id=task.id,
        org_id=task.org_id, user=user,
    )


def _attachment_download(
    session: Session, request: Request, task: Task, attachment: TaskAttachment, user: AdminUser
) -> Response:
    """`user_task_attachment_content`'s download, over the route's async session (`run_sync`)."""
    # Redirect minted only after every guard ran. Disposition is `attachment` except for a comment
    # image, signed `inline` so a thumbnail opened in a tab shows: safe because its ticket admits only
    # raster types (`COMMENT_IMAGE_CONTENT_TYPES` — no SVG), and that type is what S3 is signed to
    # answer with, so the bytes render as an image whatever they hold.
    redirect = _signed_download_redirect(
        attachment,
        for_browser=True,
        inline=attachment.kind == ATTACHMENT_KIND_COMMENT_IMAGE,
    )
    # A runner's artifact reached by this route is the same download `artifact_content` records; the
    # human's own inputs are not artifacts.
    if attachment.kind not in (ATTACHMENT_KIND_FILE, ATTACHMENT_KIND_COMMENT_IMAGE):
        data_access_events.record_committed(session, **_artifact_download(attachment, task), user=user)
    _record_attachment_read(session, request, task, attachment, user)
    return redirect


@router.get("/{task_id}/attachments", response_model=list[AttachmentOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
@on_session
def list_attachments(session: Session, task_id: int, _user: AdminUser = Depends(require_user)) -> list[AttachmentOut]:
    """The task form's attachments row — inputs only, hence the `kind` filter: an output shown here
    would be advertised as a dispatch input. Deliverables are listed by `list_artifacts`."""
    _user = adopt_user(session, _user)
    get_task_or_404(task_id, session)
    rows = session.scalars(input_attachments_select(task_id)).all()
    return [AttachmentOut.model_validate(row) for row in rows]


def input_attachments_select(task_id: int) -> Select:
    """`list_attachments`' rows: the task's uploaded `file` inputs, oldest first (a pending row would be a dead chip)."""
    inputs = select(TaskAttachment).where(TaskAttachment.task_id == task_id, TaskAttachment.kind == ATTACHMENT_KIND_FILE)
    return inputs.where(uploaded_clause()).order_by(TaskAttachment.id)


@router.get("/{task_id}/artifacts", response_model=list[ArtifactOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
@on_session
def list_artifacts(session: Session, task_id: int, _user: AdminUser = Depends(require_user)) -> list[ArtifactOut]:
    """Every artifact a runner handed back for this task, metadata only, newest first — the Artifacts
    tab's picker.

    Document kinds only (`app/task_manifest.py::DOCUMENT_KINDS`), so every line opens; derived and
    reference kinds are rendered by their own surfaces, and the human's `attachment` input is not an
    artifact. Superseded rows stay listed with `current: false`, matching the settle's latest-wins check.
    Task-scoped with no parent-epic union (an artifact is this task's own output; an epic gets its one
    server-copied report). Reset-hidden rows are excluded via `app/task_artifacts.py`, and the content
    route repeats that filter so a kept URL dies too."""
    _user = adopt_user(session, _user)
    task = get_task_or_404(task_id, session)
    return artifacts_out(visible_document_artifacts(session, task))


def artifacts_out(rows: Sequence[TaskAttachment], *, with_region: bool = True) -> list[ArtifactOut]:
    """`list_artifacts`' rows (order kept), `current` the latest of each kind among them; no `source_task_id` (an
    artifact is the task's own). `with_region=False` leaves the region unread (`routes/live_public.py` serves none)."""
    current_ids = current_artifact_ids(rows)
    return [
        ArtifactOut(
            id=row.id, kind=row.kind, filename=row.filename, content_type=row.content_type, size_bytes=row.size_bytes,
            created_at=row.created_at, current=row.id in current_ids, **(stored_region(row) if with_region else {}),
        )
        for row in rows
    ]


class ArtifactChanged(Exception):
    """`serve_artifact` read bytes carrying another ETag than `verify_etag` asked for; `etag` is theirs."""

    def __init__(self, etag: str) -> None:
        super().__init__("the artifact's bytes changed")
        self.etag = etag


def serve_artifact(
    artifact: TaskAttachment, *, download: bool, on_redirect: Callable[[], None], verify_etag: bool = False,
) -> Response:
    """One document artifact's bytes, past every guard — `artifact_content`'s delivery (its docstring carries the
    disposition and CSP argument), shared with the Live Cast viewer (`app/routes/live_public.py`). `on_redirect`
    runs once a DOWNLOAD's presigned URL is minted, before it leaves (a played media file's is a preview, not
    recorded); `artifact.s3_key` is set. `verify_etag` (the viewer's):
    proxied bytes not carrying the row's `etag` raise `ArtifactChanged` instead of being served
    (`app/live_cast/drift.py`)."""
    # Parameters stripped: the allowlist is keyed on the bare type.
    base_type = (artifact.content_type or "").split(";", 1)[0].strip().lower()
    renderable = (
        artifact.kind not in FREEFORM_KINDS or base_type in INLINE_FILE_CONTENT_TYPES
    )
    if artifact.kind in FREEFORM_KINDS and not download:
        streamed = media_redirect(artifact)
        if streamed is not None:
            # A preview, like a proxied one: not recorded as a download.
            return streamed
    # Only delivery branches here; every decision above and every header below is identical. A preview
    # is proxied because the CSP cannot be attached to an S3 response; downloads redirect (D8) so large
    # transfers skip this process. `MAX_PROXY_BYTES` bounds what may be buffered, not the upload cap.
    if download or not renderable or artifact.size_bytes > attachment_storage.MAX_PROXY_BYTES:
        # The branch that hands the bytes over by presigned URL (a download, or a preview too large to
        # proxy, recorded alike); a proxied inline preview is not recorded.
        redirect = _signed_download_redirect(artifact, for_browser=True)
        on_redirect()
        return redirect
    body, etag = _fetched_or_502(artifact)
    if verify_etag and not (etag and etag == artifact.etag):
        raise ArtifactChanged(etag)
    if base_type == "text/html" and not body.startswith(UTF16_BOMS):
        # Pinch zooms the report inside its frame, not the page around it (`app/artifact_pinch_zoom.py`).
        body += PINCH_ZOOM_SCRIPT
    return _sandboxed_inline(
        body, artifact.content_type or "application/octet-stream", artifact.filename,
        "attachment" if download or not renderable else "inline",
    )


def _fetched_or_502(row: TaskAttachment) -> tuple[bytes, str]:
    """A row's bytes and ETag for a proxied response, or the 502 that says the row survives a failed read."""
    try:
        return attachment_storage.fetch_versioned(_storage_or_502(row), row.s3_key or "")
    except attachment_storage.StorageUnavailable as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"attachment {row.id} is stored in object storage and it could not be read: "
                f"{exc} — the row is intact, so a retry is worth making before treating it as lost"
            ),
        ) from None


def _sandboxed_inline(body: bytes, media_type: str, filename: str, disposition: str) -> Response:
    """Proxied bytes under the rendering CSP — the argument for every directive is `artifact_content`'s docstring."""
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(filename, disposition),
            # See `artifact_content`'s docstring: these three do not inherit from `default-src`.
            "Content-Security-Policy": (
                "sandbox allow-scripts allow-forms; default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; "
                "base-uri 'none'; object-src 'none'; frame-ancestors 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


#: What `?preview=1` shows of a human's attachment in place: `{suffix: the type it is served as}`. Keyed on the
#: NAME's suffix, never the stored type (the uploading browser's claim), and a literal table rather than the
#: `mimetypes` lookup, which moves between CPython releases. Closed: a suffix absent here downloads. Images and
#: PDF are served as themselves; every text format as `text/plain`, so XML or JSON never reaches a parser that
#: could run script. No SVG (scriptable) and no HTML. Projected to the console by the inline-preview generator
#: (`apps/web/src/generated/inlinePreviewTypes.ts`), whose viewer picks a renderer from the served type.
PREVIEW_SUFFIX_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/plain",
    ".markdown": "text/plain",
    ".csv": "text/plain",
    ".json": "text/plain",
    ".xml": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
}


def media_redirect(row: TaskAttachment) -> RedirectResponse | None:
    """An audio or video file played in place (`MEDIA_SUFFIX_TYPES`, keyed on the NAME): a signed `inline` storage
    URL typed as its media type, at any size, so the browser streams and seeks by Range. `None` for any other name."""
    served = MEDIA_SUFFIX_TYPES.get(os.path.splitext(row.filename)[1].lower())
    if served is None:
        return None
    return _signed_download_redirect(
        row, for_browser=True, inline=True, content_type=served, ttl_seconds=attachment_storage.MEDIA_TTL_SECONDS,
    )


def attachment_preview(row: TaskAttachment) -> Response | None:
    """A human's attachment rendered in place (the console's full-screen viewer and image thumbnails), or `None`
    when its name is not a previewable format (`PREVIEW_SUFFIX_TYPES`) — the caller then answers with its download.

    Proxied under the rendering CSP while small enough (`MAX_PROXY_BYTES`). Above that, an image goes by a presigned
    `inline` redirect signed with its served type, so S3's origin renders it as an image whatever the bytes hold; a
    PDF or text file that large gets `None` (the viewer never asks): a PDF is framed, and the console's `frame-src`
    refuses S3, and text is fetched same-origin. Private-cached briefly, since a thumbnail is re-rendered on every
    remount of its row. Every serve is a PHI read at the caller, thumbnails included: an image drawn is an image read."""
    streamed = media_redirect(row)
    if streamed is not None:
        return streamed
    served = PREVIEW_SUFFIX_TYPES.get(os.path.splitext(row.filename)[1].lower())
    if served is None:
        return None
    if row.size_bytes > attachment_storage.MAX_PROXY_BYTES:
        if not served.startswith("image/"):
            return None
        return _signed_download_redirect(row, for_browser=True, inline=True, content_type=served)
    body, _etag = _fetched_or_502(row)
    response = _sandboxed_inline(
        body, "text/plain; charset=utf-8" if served == "text/plain" else served, row.filename, "inline",
    )
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@router.get("/{task_id}/artifacts/{artifact_id}/content")
@feature("tasks", "view", object_type="task", object_params=("task_id",))
async def artifact_content(
    db: DbDep,
    task_id: int,
    artifact_id: int,
    download: Annotated[
        bool,
        Query(
            description=(
                "Hand the bytes over as a file instead of rendering them in place — the operator "
                "pressed Download. EVERY kind and every content type honours it, including the "
                "free-form types the route would otherwise preview in place."
            ),
        ),
    ] = False,
    *,
    request: Request,
    _user: AdminUser = Depends(require_user),
) -> Response:
    """Serve one artifact's bytes — the sandboxed iframe's URL for a `report`, the markdown fetch for
    `prd`/`architecture`, and with `?download=1` the Download control for every kind.

    The 404 guard checks `kind` against `DOCUMENT_KINDS` as well as `task_id`: this route renders inline
    from the console origin, so a human's uploaded HTML must not be reachable through it. A Reset-hidden
    artifact also 404s (the Task is loaded for its watermark).

    The explicit CSP overrides `SecurityHeadersMiddleware`'s default (whose `frame-ancestors 'none'`
    would block the framing). `sandbox allow-scripts allow-forms` (never `allow-same-origin`) gives an
    opaque origin: inline scripts run so a mockup's click handlers work, and `allow-forms` lets a wizard's
    `<form>` fire its `submit` handler (without it the browser blocks submission before the event),
    but they reach no console cookie, storage or API; `script-src 'unsafe-inline'` admits only the document's own
    scripts; `default-src 'none'` blocks
    every subresource fetch and beacon; `style-src 'unsafe-inline'` keeps inline report styling;
    `img-src`/`font-src data:` admit self-contained assets; `frame-ancestors 'self'` restores framing
    protection; `form-action 'none'` keeps `allow-forms` from posting anywhere (the handler runs, nothing
    is sent); `form-action`, `base-uri`, `object-src` are spelled because they do not inherit from
    `default-src`. The same CSP goes on every kind with no per-type branch — measured, Chromium's PDF
    viewer renders under it (what blanks it is an iframe `sandbox` attribute in `apps/web`).

    For inline-eligible free-form types the CSP is the only guard left; it must not be weakened,
    narrowed or made conditional. (`user_task_attachment_content` relies instead on its `attachment`
    disposition, `inline` only for a raster-only comment image.)

    `Content-Disposition` answers two questions: what the bytes can render as (pinned kinds inline;
    free-form `attachment` unless its stored type is in `INLINE_FILE_CONTENT_TYPES`), and what the
    operator asked for (`?download=1` forces `attachment`, read only below the guard, so it widens
    nothing). Residual: a downloaded `report` opened from disk runs outside this sandbox, on an opaque
    origin without the console cookie — the exposure `FREEFORM_KINDS` already accepts. Accepted with
    scripts on: a script can read what a viewer types into the frame and send it, or the artifact itself,
    out by navigating the frame away or over WebRTC — neither is CSP-governed; phishing and tracking,
    never the session. The frame has no `allow-top-navigation`, `allow-popups` or `allow-modals`."""
    task, artifact, _user = await db.run_sync(_artifact_content, task_id, artifact_id, request, _user)
    # The PHI read commits only in a HIPAA org: otherwise the reads' connection goes back here.
    await release(db)
    redirected = False

    def downloaded() -> None:
        nonlocal redirected
        redirected = True

    # Off the loop, with no connection held: a proxied preview is an S3 GET.
    response = await asyncio.to_thread(serve_artifact, artifact, download=download, on_redirect=downloaded)
    if redirected:
        # Committed before the URL leaves: a failed record is a 500, never an unrecorded download.
        await db.run_sync(_record_artifact_download, task, artifact, _user)
    return response


def _artifact_content(
    session: Session,
    task_id: int,
    artifact_id: int,
    request: Request,
    _user: AdminUser,
) -> tuple[Task, TaskAttachment, AdminUser]:
    """`artifact_content`'s guards and its PHI read, over the route's async session (`run_sync`): committed;
    returns the task, the artifact, and the caller joined to the session."""
    _user = adopt_user(session, _user)
    task = get_task_or_404(task_id, session)
    # Whole entity (see `user_task_attachment_content`); bytes fetched only on the serving arm.
    artifact = get_or_404(session, TaskAttachment, artifact_id, detail="artifact not found", where=(
        TaskAttachment.task_id == task_id,
        TaskAttachment.kind.in_(DOCUMENT_KINDS),
        # Pending rows are not served (`task_artifacts.uploaded_clause`).
        uploaded_clause(),
        # Same `or_` shape as both twins, so the `kind` guard beside it cannot silently drop the
        # exemption. The exempt arm is `== ATTACHMENT_KIND_FILE` (direction).
        or_(
            TaskAttachment.kind == ATTACHMENT_KIND_FILE,
            visible_artifact_clause(TaskAttachment.created_at, task.artifacts_hidden_before),
        ),
    ))
    if not artifact.s3_key:
        # A key-less row names no bytes.
        raise HTTPException(status_code=404, detail="artifact not found")
    # Every serving branch below is a read of the artifact, the in-place view included.
    hipaa.record_phi_read(
        session, request, record_type="artifact", record_id=artifact.id, task_id=task_id,
        org_id=task.org_id, user=_user,
    )
    return task, artifact, _user


def _record_artifact_download(session: Session, task: Task, artifact: TaskAttachment, _user: AdminUser) -> None:
    """The `artifact_downloaded` record, over the route's async session (`run_sync`)."""
    data_access_events.record_committed(session, **_artifact_download(artifact, task), user=_user)


@agent_router.post("/{task_id}/artifacts", response_model=AttachmentOut, status_code=201)
@infra(Tenancy.ORG_SCOPED)
async def agent_upload_artifact(
    db: DbDep,
    task_id: int,
    kind: str = Form(...),
    file: UploadFile = File(...),
    _: None = Depends(_check_token_for_task),
) -> AttachmentOut:
    """The typed document-artifact channel a runner hands deliverables back through: `report`,
    `prd`/`architecture` (`app/persona_registry.py::PERSONA_MANIFEST`), and optional free-form `file`s.

    `kind` rides as a form field. Buffers the body, so it is held at `MAX_BUFFERED_ATTACHMENT_SIZE`
    (mirrored by `apps/jaahd/cred_broker.py::_MAX_BUFFERED_UPLOAD_BYTES`). Agent-token authenticated;
    `uploaded_by_id` is the task's original executor (`task_ancestry.executor_user_id`).
    Pending-then-stamped via `_store_whole_file`. Not a fallback for a bucketless plane — it refuses with
    the ticket door. Validation is `_validated_artifact`, shared with `agent_artifact_ticket`, and
    `content_type` is never taken from the part.

    Documents only: merged-PR urls and no-PR reasons belong to the settle (`tasks.outcome_pr_url` keeps
    one writer). Append-only: latest-wins is answered at read time
    (`app/task_artifacts.py::current_artifact`), with no unique constraint, which is what lets many
    free-form `file`s coexist."""
    filename, content_type, cap = await db.run_sync(_agent_upload_artifact, task_id, kind, file.filename)
    # A read so far: its connection goes back before the body is buffered (off the loop: a spooled file).
    await release(db)
    content = await asyncio.to_thread(_read_bounded, file.file, cap)
    where, key, subject_id, artifact = await db.run_sync(
        _stage_artifact, task_id, kind, filename, content_type, content
    )
    etag = await asyncio.to_thread(_put_whole_file, where, key, content, subject_id)
    return await db.run_sync(_stamped_out, artifact, etag)


def _agent_upload_artifact(
    session: Session,
    task_id: int,
    kind: str,
    upload_filename: Optional[str],
) -> tuple[str, str, int]:
    """`agent_upload_artifact`'s checks before the body is read, over the route's async session (`run_sync`): the
    stored filename and type, and the task's file cap."""
    task = get_task_or_404(task_id, session)
    _refuse_if_epic(task, task_id)
    filename, content_type = _validated_artifact(kind, upload_filename)
    # Asked before reading the body (as in `upload_attachment`).
    _storage_or_503(task_id)
    return filename, content_type, _task_file_cap(session, task_id)


def _stage_artifact(
    session: Session,
    task_id: int,
    kind: str,
    filename: str,
    content_type: str,
    content: bytes,
) -> tuple[attachment_storage.Target, str, int, TaskAttachment]:
    """`agent_upload_artifact`'s pending row, over the route's async session (`run_sync`): `_stage_whole_file`."""
    artifact = TaskAttachment(
        task_id=task_id,
        kind=kind,
        filename=filename,
        content_type=content_type,
        size_bytes=len(content),
        # The original human behind the runner (starter, else creator — D3b), or NULL when the chain names none.
        uploaded_by_id=executor_user_id(session, task_id),
    )
    where, key, subject_id = _stage_whole_file(session, artifact, content)
    return where, key, subject_id, artifact


@agent_router.post(
    "/{task_id}/artifacts/ticket", response_model=UploadTicketOut, status_code=201
)
@infra(Tenancy.ORG_SCOPED)
@on_session
def agent_artifact_ticket(
    session: Session,
    task_id: int,
    request: Request,
    body: ArtifactTicketIn = Depends(_artifact_ticket_body),
    _: None = Depends(_check_token_for_task),
) -> UploadTicketOut:
    """The runner's presigned door: declare the artifact, get a pending row and a URL to PUT it at
    (`apps/jaahd/session_sweep.py::put_object` sends the bytes; D4).

    Every refusal the multipart door makes is made here first, before a bearer URL and key are minted
    (`_validated_artifact`, `_refuse_if_epic`). The declared-size cap refuses early; `/confirm` replaces
    the claim with what S3 received."""
    task = get_task_or_404(task_id, session)
    _refuse_if_epic(task, task_id)
    filename, content_type = _validated_artifact(body.kind, body.filename)
    ticket = _mint_ticket(
        session,
        task_id=task_id,
        kind=body.kind,
        filename=filename,
        content_type=content_type,
        size_bytes=body.size_bytes,
        # The original human behind the runner (D3b), as at the multipart door.
        uploaded_by_id=executor_user_id(session, task_id),
        # The node dials S3 directly, so it gets the absolute URL (see `_mint_ticket`).
        for_browser=False,
    )
    if ticket.upload_url.startswith("/"):
        # The community edition's volume signs a path on this control plane: absolute as the node dialed it, as the
        # session store's ticket is (`routes/agent_sessions.py::upload_ticket`). The node's uploader takes only
        # an http(s) URL (`apps/jaahd/session_sweep.py::_put_object`).
        ticket.upload_url = str(request.base_url).rstrip("/") + ticket.upload_url
    return ticket


@agent_router.post(
    "/{task_id}/artifacts/{artifact_id}/confirm", response_model=AttachmentOut
)
@infra(Tenancy.ORG_SCOPED)
async def agent_confirm_artifact(
    db: DbDep,
    task_id: int,
    artifact_id: int,
    _: None = Depends(_check_token_for_task),
) -> AttachmentOut:
    """The second half of `agent_artifact_ticket`: confirms the bytes landed and makes the row visible.

    `DOCUMENT_KINDS` only: an agent token may settle a runner's deliverable, never a human's pending
    input. The rest is `_complete_pending`."""
    return await _complete_pending(
        db,
        task_id=task_id,
        attachment_id=artifact_id,
        kinds=tuple(DOCUMENT_KINDS),
    )
