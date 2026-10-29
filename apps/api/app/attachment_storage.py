"""Where a task attachment's bytes live, and how they move.

The bucket is the session-transcript bucket under `task-attachments/` (D1), inheriting its versioning,
SSE, public-access block, TLS-only policy and lifecycle (all set by the deployment's Terraform) — the
org's data region's (`app/data_residency.py`): a write goes to `session_transcript_archive.org_bucket`,
the row records it (`TaskAttachment.s3_bucket`), and every read, delete and copy targets the row's own
(`target(bucket=...)`).

The control plane never carries the bytes (D4): it presigns, the browser or node PUTs, and a download
is a redirect to S3 — a large transfer through the single API process would stall everything behind it.
Inline previews do not redirect (D8): they are fetched and served from our origin so the response can
carry the sandboxing CSP (`app/routes/attachments.py::artifact_content`).

The bucket is the only store in the cloud. Off until configured: `target()` returns None, which callers turn
into a 503 on writes (`_storage_or_503`) and a 502 on reads (`_storage_or_502`) — a shipped state, not a 500.
The community edition has no S3 (no `aws` feature) and keeps the bytes on its own volume instead (`LOCAL_ROOT`): the same calls, the
same flow, with the control plane's own signed URLs standing in for S3's (`app/routes/ce_storage.py`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Optional
from urllib.parse import quote, urlencode
from uuid import UUID

import httpx

from app import ce_bootstrap, edition_features, session_transcript_archive, session_transcripts
from app.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.aws.container_credentials import AwsCredentials

log = logging.getLogger("app.attachment_storage")

#: The key prefix inside the shared bucket. One segment, disjoint from `app/session_transcripts.py`'s
#: `transcripts/` and `artifacts/`. The CP never lists the bucket.
KEY_PREFIX = "task-attachments"

#: How long an upload URL lives: S3 checks it when the PUT starts, so a multi-GB body may run past it; short
#: enough that a leaked URL is not a standing grant. Matches `app/session_transcripts.py::LEASE_TTL`.
UPLOAD_TTL_SECONDS = 15 * 60

#: How long a download redirect lives. The browser follows it immediately.
DOWNLOAD_TTL_SECONDS = 5 * 60

#: Ceiling on bytes buffered in memory for an inline preview — not the storage cap (`MAX_ATTACHMENT_SIZE`).
#: The frontend previews text only up to 2 MB (`artifactKinds.ts` `TEXT_PREVIEW_MAX_BYTES`); anything
#: larger is a redirected download.
MAX_PROXY_BYTES = 8 * 1024 * 1024

#: Bounded preview fetch timeouts, so a stalled S3 connection cannot pin a threadpool worker.
_FETCH_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

#: The path prefix the browser gets instead of a self-hosted endpoint's origin (local stack only: MinIO
#: has no host port). The dev server proxies `/s3` with `changeOrigin: true`, restoring `Host` so the
#: SigV4 signature stays valid; the browser code PUTs whatever URL the ticket gave it in both environments.
BROWSER_ENDPOINT_PATH = "/s3"

#: The community edition's store: a directory on its own volume, keyed like the bucket.
LOCAL_ROOT = ce_bootstrap.CE_DATA_DIR / "attachments"
#: The community edition's session store (event chunks, transcripts, manifests): its own
#: volume, keyed like the bucket's `transcripts/` and `artifacts/` (`app/session_transcripts.py`).
SESSIONS_ROOT = ce_bootstrap.CE_DATA_DIR / "sessions"
#: What a row records as its bucket when its bytes are on that volume.
LOCAL_BUCKET = "local"
#: Where the control plane serves the volume's signed PUTs and GETs (`app/routes/ce_storage.py`).
CE_STORAGE_PATH = "/api/ce/storage"
# A key the volume accepts: what `object_key` and the other key builders produce (a transcript's `dt=` segment
# included), never `..` or an absolute path.
_LOCAL_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]*(/[A-Za-z0-9][A-Za-z0-9._=-]*)*")


class StorageUnavailable(RuntimeError):
    """S3 is configured but the operation could not be completed — never raised for "not configured".

    Unconfigured is a shipped state (503/502); a configured bucket erroring is a fault an operator must see.
    """


#: An S3 operation reached in an edition without `aws`: unreachable, since `target()` is the local volume there.
_NO_S3 = "no S3 in this edition (its objects are on the local volume)"


@dataclass(frozen=True)
class Target:
    """Everything one S3 call needs, resolved once. `endpoint` is empty in every deployed environment."""

    region: str
    bucket: str
    credentials: Optional["AwsCredentials"]
    endpoint: str
    #: The community edition's volume directory; empty for S3. Set, every call below reads and writes files.
    root: str = ""


def target(bucket: Optional[str] = None) -> Optional[Target]:
    """The bucket, region and credentials to use, or `None` when attachment storage is not configured.

    `bucket` is a row's `s3_bucket` (a read, delete or copy) or the org's (`session_transcript_archive.org_bucket`,
    a write); its region is that bucket's own (`session_transcript_archive.located`). None = the configured bucket
    and region, which is also what a legacy row's NULL means.

    The community edition's volume (`local_volume`) in an edition without `aws`, whatever `bucket` a row recorded.
    Every caller must answer for `None` (see the module docstring). Its two causes (no bucket/region, no
    credentials) already warn at their source, so they are not distinguished here.
    """
    if not edition_features.enabled("aws"):
        # No S3 in this edition: every object, whatever bucket a row recorded, is on its volume.
        return local_volume()
    found = session_transcript_archive.located(bucket)
    if found is None:
        return None
    region, bucket, credentials = found
    return Target(
        region=region,
        bucket=bucket,
        credentials=credentials,
        endpoint=get_settings().s3_endpoint_url,
    )


def container_target(bucket: Optional[str], *, unresolved: str) -> Optional[Target]:
    """An off-until-configured bucket (the evidence bucket, the security archive) as a target under the
    CONTAINER's credentials, or `None` when `bucket` or `AWS_REGION` is unset — the deployment has not turned it
    on. A bucket set with no credentials resolving is a fault, not an off state: `RuntimeError(unresolved)`.
    In an edition without `aws` a set bucket is that fault too: it has no S3 to reach it with."""
    if not edition_features.enabled("aws"):
        if bucket:
            raise RuntimeError(unresolved)
        return None
    from app.aws.container_credentials import resolve_credentials

    settings = get_settings()
    if not (bucket and settings.aws_region):
        return None
    credentials = resolve_credentials()
    if credentials is None:
        raise RuntimeError(unresolved)
    return Target(region=settings.aws_region, bucket=bucket, credentials=credentials, endpoint=settings.s3_endpoint_url)


def local_volume(key: str = "") -> Target:
    """The community edition's volume `key` is stored on: the session store for a session object's key
    (`session_transcripts.TRANSCRIPT_ROOT`/`ARTIFACT_ROOT`), else the attachments'."""
    return Target(
        region="", bucket=LOCAL_BUCKET, credentials=None, endpoint="",
        root=str(SESSIONS_ROOT if _is_session_key(key) else LOCAL_ROOT),
    )


def _is_session_key(key: str) -> bool:
    return key.split("/", 1)[0] in (session_transcripts.TRANSCRIPT_ROOT, session_transcripts.ARTIFACT_ROOT)


def object_key(
    *,
    task_id: Optional[int] = None,
    attachment_id: int,
    ticket_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    automation_id: Optional[UUID] = None,
    org_template_id: Optional[UUID] = None,
) -> str:
    """The key for one attachment's bytes: `task-attachments/<task_id>/<attachment_id>`,
    `task-attachments/tickets/<ticket_id>/<attachment_id>` for a support ticket's file, or
    `task-attachments/support-chats/<conversation_id>/<attachment_id>` for a file pasted into a support chat
    — inside the same prefix and so the same IAM grant — or `task-attachments/automations/<uuid>/<id>`
    for a file on an automation's Task action, `task-attachments/org-templates/<uuid>/<id>` for an image of an org's
    own task template. Exactly one subject id is given.

    Derived from the row only (as `session_transcripts.reject_supplied_key` enforces for nodes): no client
    names a key, so there is no traversal question. The filename is not in the key; it rides
    `Content-Disposition`. Integer ids cannot yield the segments `presign.s3_object_url` refuses.
    """
    subjects = [s for s in (task_id, ticket_id, conversation_id, automation_id, org_template_id) if s is not None]
    if len(subjects) != 1:
        raise ValueError(
            "exactly one of task_id, ticket_id, conversation_id, automation_id and org_template_id names an "
            "attachment's subject"
        )
    if automation_id is not None or org_template_id is not None:
        if attachment_id <= 0:
            raise ValueError("attachment_id must be a positive database id")
        if org_template_id is not None:
            return f"{KEY_PREFIX}/org-templates/{UUID(str(org_template_id))}/{attachment_id}"
        return f"{KEY_PREFIX}/automations/{UUID(str(automation_id))}/{attachment_id}"
    if subjects[0] <= 0 or attachment_id <= 0:
        raise ValueError("the subject id and attachment_id must be positive database ids")
    if ticket_id is not None:
        return f"{KEY_PREFIX}/tickets/{ticket_id}/{attachment_id}"
    if conversation_id is not None:
        return f"{KEY_PREFIX}/support-chats/{conversation_id}/{attachment_id}"
    return f"{KEY_PREFIX}/{task_id}/{attachment_id}"


def template_asset_key(*, template_id: UUID, asset_id: UUID) -> str:
    """The key for one image of a GLOBAL task template (`task_template_assets`): `task-attachments/templates/<template
    uuid>/<asset uuid>`, inside the attachments' prefix and grant, derived from the row as `object_key` is."""
    return f"{KEY_PREFIX}/templates/{UUID(str(template_id))}/{UUID(str(asset_id))}"


def object_url(where: Target, key: str) -> str:
    if not edition_features.enabled("aws"):
        raise StorageUnavailable(_NO_S3)
    from app.aws import presign  # S3 only (the `aws` feature); a local Target never reaches it.

    return presign.s3_object_url(
        bucket=where.bucket, key=key, region=where.region, endpoint=where.endpoint
    )


def _sign(
    where: Target,
    *,
    method: str,
    url: str,
    expires_seconds: int,
    content_length: Optional[int] = None,
    content_md5: Optional[str] = None,
) -> str:
    if where.credentials is None:
        raise StorageUnavailable("this store has no S3 credentials to sign with (the local volume signs its own)")
    if not edition_features.enabled("aws"):
        raise StorageUnavailable(_NO_S3)
    from app.aws import presign

    return presign.presigned_url(
        method=method,
        url=url,
        region=where.region,
        service="s3",
        credentials=where.credentials,
        amz_date=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        expires_seconds=expires_seconds,
        content_length=content_length,
        content_md5=content_md5,
        # Only when `endpoint` (`S3_ENDPOINT_URL`, empty in every deployed environment) is set.
        allow_insecure=bool(where.endpoint),
    )


def browser_url(where: Target, url: str) -> str:
    """The signed URL as the browser should dial it — identical to `url` in every deployed environment.

    Only a self-hosted endpoint diverges; see `BROWSER_ENDPOINT_PATH`. The local volume's URLs are already
    paths on this origin.
    """
    if not where.endpoint:
        return url
    if not edition_features.enabled("aws"):
        raise StorageUnavailable(_NO_S3)
    from app.aws import presign

    # Compare against the normalized origin `object_url` actually built (lowercased host), not the raw
    # setting, or a mixed-case endpoint would hand the browser an undialable absolute URL.
    origin = presign.s3_endpoint_origin(where.endpoint)
    if url.startswith(origin):
        return BROWSER_ENDPOINT_PATH + url[len(origin):]
    return url


def presign_upload(where: Target, key: str, *, content_length: int) -> tuple[str, datetime]:
    """A URL authorizing one PUT of `key` at exactly `content_length` bytes, and when it expires. The URL is
    a bearer token — never log, raise or store it.

    The signed length makes the cap a cap: without it one ticket authorizes a PUT of any size, replayable
    for its TTL. The bucket refuses an over-cap body before storing it (`presign.SIGNABLE_CONTENT_LENGTH`).
    `_mint_ticket` enforces the cap before reaching here.
    """
    if content_length < 0:
        # The cap is not re-checked here; its home is `app/routes/attachments.py::MAX_ATTACHMENT_SIZE`, enforced
        # by `_mint_ticket`. Only a negative length is refused here.
        raise ValueError("content_length must not be negative")
    if where.root:
        return _local_url("PUT", key, UPLOAD_TTL_SECONDS, length=str(content_length)), datetime.now(
            timezone.utc
        ) + timedelta(seconds=UPLOAD_TTL_SECONDS)
    url = _sign(
        where,
        method="PUT",
        url=object_url(where, key),
        expires_seconds=UPLOAD_TTL_SECONDS,
        content_length=content_length,
    )
    return url, datetime.now(timezone.utc) + timedelta(seconds=UPLOAD_TTL_SECONDS)


def presign_download(
    where: Target, key: str, *, filename: str = "", content_type: str = "", inline: bool = False
) -> str:
    """A URL authorizing one GET of `key`, with S3 told what to call the file and what to type it as.

    `response-content-disposition`/`response-content-type` are signed, so the client cannot alter them.
    `presign.presigned_url` requires params already in AWS `UriEncode` form; `quote(safe="")` provides it.
    """
    # `attachment` unless the caller vouches for inert bytes (`inline`); only the filename is optional. On
    # the redirect arm this disposition is the only thing stopping S3's origin from rendering agent-authored
    # HTML inline (our CSP cannot be attached there). Enforced here, not upstream, because this is the last
    # step before a browser. The `inline` callers: a comment image, raster by its ticket
    # (`app/routes/attachments.py::user_task_attachment_content`), and a task template's raster image, typed from
    # its bytes (`app/routes/task_templates.py::template_asset_content`).
    parameters = ["response-content-disposition="]
    disposition = "inline" if inline else "attachment"
    if filename:
        # Both parameters, as `app/routes/agent/task_reads.py::_content_disposition` emits on the proxied arm, so the saved
        # filename is the same whichever arm served it. Escaping is argued in that helper.
        safe = "".join(character if 0x20 <= ord(character) < 0x7F else "_" for character in filename)
        safe = safe.replace("\\", "_").replace('"', "_")
        # Double percent-encoding is correct: `filename*` (RFC 5987) is percent-encoded, then the whole
        # disposition is encoded again as a query parameter; S3 decodes once. Collapse either layer and
        # non-ASCII names arrive mojibake.
        disposition += f"; filename=\"{safe}\"; filename*=UTF-8''{quote(filename, safe='')}"
    parameters[0] += quote(disposition, safe="")
    if content_type:
        parameters.append("response-content-type=" + quote(content_type, safe=""))
    if where.root:
        return _local_url("GET", key, DOWNLOAD_TTL_SECONDS, disposition=disposition, content_type=content_type)
    # No emptiness guard: the disposition is always seeded above.
    url = object_url(where, key) + "?" + "&".join(parameters)
    return _sign(where, method="GET", url=url, expires_seconds=DOWNLOAD_TTL_SECONDS)


def _request(
    where: Target, *, method: str, key: str, body: Optional[bytes] = None, content_md5: Optional[str] = None,
) -> httpx.Response:
    """One presigned request the control plane makes for itself, sent with plain httpx.

    No SDK (the `aws` package's docstring says why). `presign.reject_forbidden_headers` checks the assembled
    request because httpx adds headers and a presigned request signs only `host` (plus `content_md5`); an
    extra `x-amz-*` or `content-type` is an opaque 403. `content_md5` is the one header a caller adds, and it
    is SIGNED and sent together: S3 403s a presigned request carrying any header its signature does not cover.
    """
    if not edition_features.enabled("aws"):
        raise StorageUnavailable(_NO_S3)
    from app.aws import presign

    url = _sign(
        where, method=method, url=object_url(where, key), expires_seconds=DOWNLOAD_TTL_SECONDS,
        content_md5=content_md5,
    )
    headers = {"Content-MD5": content_md5} if content_md5 is not None else None
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
            request = client.build_request(method, url, content=body, headers=headers)
            # httpx sets `content-type` on a request with a body; a presigned request must send none.
            request.headers.pop("content-type", None)
            presign.reject_forbidden_headers(request.headers.keys())
            return client.send(request)
    except httpx.HTTPError as exc:
        # Never let the URL escape: httpx exception messages carry it, with a live key id and session token.
        raise StorageUnavailable(f"{type(exc).__name__} during an S3 {method}") from None


def fetch(where: Target, key: str, *, timeout: Optional[float] = None) -> bytes:
    """The object's bytes, for a response the control plane must serve from its own origin.

    Bounded by `MAX_PROXY_BYTES` before allocation: a streamed read refuses a declared length up front and
    the running total mid-stream, since `Content-Length` is only a claim. `timeout` caps each of
    `_FETCH_TIMEOUT`'s phases, for a caller with a deadline of its own (the support chat's turn).
    """
    return fetch_versioned(where, key, timeout=timeout)[0]


def fetch_versioned(where: Target, key: str, *, timeout: Optional[float] = None) -> tuple[bytes, str]:
    """`fetch`, with the `ETag` of the very response the bytes came from — so a reader that judges the bytes
    can tell whether they are still the ones a row recorded (`app/live_cast/evaluate.py`). `""` when S3 sent none.
    """
    if where.root:
        path = local_path(where, key)
        if not path.is_file():
            raise StorageUnavailable("the local store has no such object")
        if path.stat().st_size > MAX_PROXY_BYTES:
            raise StorageUnavailable(f"object is over the {MAX_PROXY_BYTES} byte preview ceiling")
        body = path.read_bytes()
        return body, _local_etag(hashlib.md5(body).hexdigest())
    if not edition_features.enabled("aws"):
        raise StorageUnavailable(_NO_S3)
    from app.aws import presign

    url = _sign(
        where, method="GET", url=object_url(where, key), expires_seconds=DOWNLOAD_TTL_SECONDS
    )
    limits = _FETCH_TIMEOUT if timeout is None else httpx.Timeout(
        connect=min(_FETCH_TIMEOUT.connect, timeout), read=min(_FETCH_TIMEOUT.read, timeout),
        write=min(_FETCH_TIMEOUT.write, timeout), pool=min(_FETCH_TIMEOUT.pool, timeout),
    )
    try:
        with httpx.Client(timeout=limits, follow_redirects=False) as client:
            request = client.build_request("GET", url)
            request.headers.pop("content-type", None)
            presign.reject_forbidden_headers(request.headers.keys())
            # `try/finally`, not `with`: a streamed response is not a context manager on every supported httpx.
            response = client.send(request, stream=True)
            try:
                if response.status_code != 200:
                    raise StorageUnavailable(
                        f"S3 answered HTTP {response.status_code} for a preview GET"
                    )
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_PROXY_BYTES:
                    raise StorageUnavailable(
                        f"object is {declared} bytes, over the {MAX_PROXY_BYTES} byte preview ceiling"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_PROXY_BYTES:
                        raise StorageUnavailable(
                            f"object exceeded the {MAX_PROXY_BYTES} byte preview ceiling mid-stream"
                        )
                    chunks.append(chunk)
                etag = response.headers.get("etag", "")
            finally:
                response.close()
    except httpx.HTTPError as exc:
        # Never let the URL escape (see `_request`).
        raise StorageUnavailable(f"{type(exc).__name__} during an S3 GET") from None
    return b"".join(chunks), etag


class Landed(NamedTuple):
    """What a HEAD says of an object: its size, and its `ETag` verbatim (quotes included; `""` when S3 sent none).

    The ETag names the BYTES, not the key: a same-length overwrite through a still-valid upload URL changes it
    and nothing else, so it is what a row records to tell its bytes from a swap (`TaskAttachment.etag`)."""

    size: int
    etag: str


def head(where: Target, key: str) -> Optional[Landed]:
    """The object's size and ETag, or `None` when it is not there.

    Confirms an upload landed: the client PUTs to S3 directly, so this is the CP's only evidence.
    """
    if where.root:
        path = local_path(where, key)
        return Landed(size=path.stat().st_size, etag=_file_etag(path)) if path.is_file() else None
    response = _request(where, method="HEAD", key=key)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise StorageUnavailable(f"S3 answered HTTP {response.status_code} for a HEAD")
    try:
        size = int(response.headers.get("content-length", ""))
    except ValueError:
        raise StorageUnavailable("S3 returned a HEAD with no usable Content-Length") from None
    return Landed(size=size, etag=response.headers.get("etag", ""))


def exists(where: Target, key: str) -> bool:
    """Whether the object is there, for a key that may never have been written.

    The CP holds `GetObject` but no `ListBucket`, so S3 answers a missing key 403, not 404 — `head` would
    raise. A 200 is proof the object exists, since the CP may read every object it may name."""
    if where.root:
        return local_path(where, key).is_file()
    response = _request(where, method="HEAD", key=key)
    if response.status_code == 200:
        return True
    if response.status_code in (403, 404):
        return False
    raise StorageUnavailable(f"S3 answered HTTP {response.status_code} for a HEAD")


def put(where: Target, key: str, body: bytes, *, content_md5: bool = False) -> str:
    """Upload `body` from this process — only for the whole-file doors — and return the stored object's ETag
    (`""` when S3 sent none; see `Landed`).

    Used by `app/routes/attachments.py::_store_whole_file`, whose caller already holds the bounded bytes;
    every ordinary upload is presigned. `content_md5` sends the body's digest, which S3 requires of any
    upload into a bucket with an Object Lock default retention (`app/security_archive.py`). It is signed:
    unsigned, S3 refuses the PUT 403 before authorizing it (see `presign.SIGNABLE_CONTENT_MD5`).
    """
    if where.root:
        return write_local(where, key, body)
    digest = base64.b64encode(hashlib.md5(body).digest()).decode("ascii") if content_md5 else None
    response = _request(where, method="PUT", key=key, body=body, content_md5=digest)
    if not 200 <= response.status_code < 300:
        # 2xx or nothing: a 3xx from a wrong-region endpoint stored nothing.
        raise StorageUnavailable(f"S3 answered HTTP {response.status_code} for a PUT")
    return response.headers.get("etag", "")


def delete(where: Target, key: str) -> None:
    """Remove one object. Best-effort by contract — see `app/routes/attachments.py::delete_attachment`."""
    if where.root:
        local_path(where, key).unlink(missing_ok=True)
        return
    response = _request(where, method="DELETE", key=key)
    # S3 answers 204 whether or not the key existed.
    if response.status_code not in (200, 204, 404):
        raise StorageUnavailable(f"S3 answered HTTP {response.status_code} for a DELETE")


def reap_unreferenced_object(session: Session, key: str, bucket: Optional[str] = None) -> bool:
    """Delete the S3 object behind a deleted row — only once no surviving row names the same key, and
    never at the cost of the delete the operator asked for. `bucket` is the deleted row's `s3_bucket`, read with
    its key before the delete (rows sharing a key share it: a copy carries both).

    Shared keys are real: an epic's final report shares a child's key
    (`routes/tasks/epic_finish.py::_publish_epic_final_report`; `TaskAttachment.s3_key` is deliberately not unique),
    and a task started from a task template shares the template's image key (`app/task_template_assets.py`). So this
    is the one reaper for both stores: a `task_attachments` row or a `task_template_assets` row naming the key keeps
    the object. A GLOBAL template's image is shared by tasks of every org, so the check reads the fleet
    (`_key_still_referenced`), never the caller's org alone. Every caller commits its delete first.

    Best effort: the row is already gone, so a failure only orphans an object (logged at WARNING);
    `except Exception` because nothing past the point of no return may reach the client.

    Returns whether the object was actually deleted."""
    try:
        if _key_still_referenced(key):
            return False
        where = target(bucket)
        if where is None:
            return False
        delete(where, key)
        return True
    except Exception:
        # The key is safe to log (derived, no credential); the URL is not.
        log.warning("orphaned stored object %s: its row is deleted but S3 still holds it", key, exc_info=True)
        return False


def _key_still_referenced(key: str) -> bool:
    """Whether any surviving row, in any org or in either store, names `key` — on a fleet session of its own, as the
    caller's org-bound session would miss another org's task sharing a global template's image. Existence only."""
    # Deferred, so importing the storage layer does not pull in the ORM.
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.task_extras import TaskAttachment
    from app.models.task_templates import TaskTemplateAsset
    from app.tenancy import seam

    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        for column in (TaskAttachment.s3_key, TaskTemplateAsset.s3_key):
            if fleet.scalars(select(column).where(column == key).limit(1)).first() is not None:
                return True
    return False


# ── The community edition's volume ──────────────────────────────────────────────────────────────────────


#: Bytes read or written per step, so neither an upload nor a checksum ever holds a whole object in memory.
_CHUNK = 1024 * 1024


def local_path(where: Target, key: str) -> Path:
    """`key`'s file under the volume. Refuses a key that could name anything outside it, a planted symlink
    included. A local Target's session key is on the session volume (`local_volume`), so a caller holding `target()` — offboarding,
    the retention sweep — reaches a session object where it lives."""
    if not _LOCAL_KEY.fullmatch(key) or ".." in key.split("/"):
        raise ValueError("not an attachment key")
    root = Path(SESSIONS_ROOT if where.bucket == LOCAL_BUCKET and _is_session_key(key) else where.root).resolve()
    path = root / key
    if not path.resolve().is_relative_to(root):
        raise ValueError("not an attachment key")
    return path


def _local_etag(md5_hex: str) -> str:
    # S3's shape for a single-part upload: the body's MD5 in quotes, so a row compares either store's alike.
    return f'"{md5_hex}"'


def _file_etag(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return _local_etag(digest.hexdigest())


class LocalUpload:
    """One object being written to the volume a chunk at a time: a private scratch file, swapped in by rename on
    `commit` (so a reader never sees half an object) and removed on `abort`."""

    def __init__(self, where: Target, key: str) -> None:
        self.path = local_path(where, key)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scratch = self.path.with_name(f".{self.path.name}.{os.getpid()}.{time.monotonic_ns()}.part")
        self._file = os.fdopen(os.open(self._scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        self._digest = hashlib.md5()
        self.size = 0

    def write(self, chunk: bytes) -> None:
        self._file.write(chunk)
        self._digest.update(chunk)
        self.size += len(chunk)

    def commit(self) -> str:
        """Swap the object in and return its ETag."""
        self._file.close()
        os.replace(self._scratch, self.path)
        return _local_etag(self._digest.hexdigest())

    def abort(self) -> None:
        self._file.close()
        self._scratch.unlink(missing_ok=True)


def write_local(where: Target, key: str, body: bytes) -> str:
    """Store `body` at `key` and return its ETag."""
    upload = LocalUpload(where, key)
    try:
        upload.write(body)
    except BaseException:
        upload.abort()
        raise
    return upload.commit()


def _local_signing_key() -> bytes:
    """A domain-separated subkey of the session secret: a URL's signature can stand for nothing else."""
    from app.auth import resolve_session_secret

    return hmac.new(resolve_session_secret().encode(), b"jaah-ce-storage-v1", hashlib.sha256).digest()


def local_signature(method: str, key: str, expires: str, **signed: str) -> str:
    """The signature over one request's method, key, expiry and every other signed parameter."""
    message = "\n".join([method, key, expires, *(f"{name}={signed[name]}" for name in sorted(signed))])
    return hmac.new(_local_signing_key(), message.encode(), hashlib.sha256).hexdigest()


def _local_url(method: str, key: str, ttl_seconds: int, **signed: str) -> str:
    """A path on this origin authorizing one `method` of `key` until it expires — a bearer token, as S3's is."""
    expires = str(int(time.time()) + ttl_seconds)
    query = {**signed, "expires": expires, "signature": local_signature(method, key, expires, **signed)}
    return f"{CE_STORAGE_PATH}/{key}?{urlencode(query, quote_via=quote)}"


def local_url_is_valid(method: str, key: str, params: dict[str, str], names: tuple[str, ...]) -> bool:
    """Whether `params` (the request's query) carry a live, matching signature for `method` of `key`; `names` are
    the other parameters that method signs."""
    expires = params.get("expires", "")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    signed = {name: params.get(name, "") for name in names}
    expected = local_signature(method, key, expires, **signed)
    return hmac.compare_digest(expected, params.get("signature", ""))
