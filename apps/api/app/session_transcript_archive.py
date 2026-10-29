"""On-demand `.tar.gz` assembly of one session's transcript, streamed from presigned S3 GETs.

Every public entry point is synchronous, a correctness constraint: the CP is one asyncio process and
gzip/tarfile hold the GIL, so assembly on the loop would stall the node bus, PTY and heartbeats. The
route is `def` and `stream_archive` a sync generator, which starlette runs via `iterate_in_threadpool`.

Concurrency is capped by a non-blocking `BoundedSemaphore`. `Permit` is single-shot because a stream
has two release paths (generator `finally`, `BackgroundTask`) and `release()` increments
unconditionally. The control plane only presigns: no S3 API, plain httpx. The community edition (no `aws`) keeps
every session object on its own volume instead (`on_volume`, `attachment_storage.local_volume`): the same keys, read
as files.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import threading
import time
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generator, Iterator, Mapping, Optional

import httpx

from app import edition_features
from app import session_transcripts as transcripts
from app.clock import as_utc

# Each assembly holds the GIL through gzip for its whole life; the cap is about CPU, not S3.
MAX_CONCURRENT_ASSEMBLIES = 2

# What a refused request is told to wait. Rendered into `Retry-After` verbatim.
RETRY_AFTER_SECONDS = 30

# Stamped once per plan; an upper bound only, since `resolve_credentials` refuses a credential inside
# its 16-minute refresh margin. A slower download aborts mid-stream.
PRESIGN_EXPIRES_SECONDS = 3600

# Read timeout per chunk, not per member: a large transcript legitimately takes minutes.
FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

# `FETCH_TIMEOUT` is per chunk, so a trickling read or slow client could pin a permit forever.
ASSEMBLY_DEADLINE_SECONDS = 30 * 60

# The manifest is read whole: 256 members at ~200 bytes, with headroom. One oversized object would
# OOM the single-process CP.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

# zlib's ratio ceiling is ~1032:1, so an unbounded call on a 64 KiB read allocates ~67 MB.
_MAX_INFLATE_CHUNK = 1024 * 1024

# Level 6: past it, more CPU (the scarce resource) buys little ratio.
GZIP_LEVEL = 6

# `zlib` needs this window size to read a gzip container rather than a raw deflate stream.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

# Members are stored gzipped and land in the tar plain; the `.tar.gz` already compresses.
_STORED_SUFFIX = ".gz"

# The manifest travels inside the archive as its provenance and integrity record.
_MANIFEST_ARCNAME = transcripts.MANIFEST_NAME


class _Deadline:
    """The wall-clock bound on one assembly, checked between members and between chunks."""

    __slots__ = ("_expires", "_seconds")

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._expires = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self._expires:
            raise ArchiveAborted(
                f"assembly exceeded its {self._seconds:g}s bound — the permit is not held longer"
            )


class ArchiveAborted(Exception):
    """A member could not be delivered intact, raised from inside the stream.

    Raising it mid-stream is deliberate: the tar trailer is never written, so the client sees a
    transport error instead of a clean EOF over a truncated archive.
    """


class ManifestUnreadable(Exception):
    """The session's `manifest.json` could not be fetched or parsed — raised before any byte is sent."""


class Permit:
    """One assembly slot, releasable exactly once however many paths try.

    `BoundedSemaphore` is then the backstop: a residual double-release is a loud `ValueError`
    rather than a cap that silently grew.
    """

    __slots__ = ("_semaphore", "_lock", "_released")

    def __init__(self, semaphore: threading.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._lock = threading.Lock()
        self._released = False

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def release(self) -> bool:
        """Free the slot. Returns True iff THIS call did it; every later call is a no-op."""
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._semaphore.release()
        return True


_PERMITS = threading.BoundedSemaphore(MAX_CONCURRENT_ASSEMBLIES)


def acquire_permit() -> Optional[Permit]:
    """A permit, or None when every slot is taken. NEVER blocks — a queued request is a stalled one."""
    if not _PERMITS.acquire(blocking=False):
        return None
    return Permit(_PERMITS)


@dataclass(frozen=True)
class Member:
    """One manifest member, resolved to everything the stream needs and nothing it does not."""

    name: str          # the manifest's own name, e.g. `main.jsonl.gz`
    arcname: str       # where it lands inside the archive, e.g. `session-<id>/main.jsonl`
    url: str           # presigned GET — A BEARER TOKEN; never log it. Empty for a member on the volume.
    bytes_raw: int     # UNCOMPRESSED size; tarfile needs it in the header before the bytes exist
    sha256: str        # over the same pre-gzip bytes
    path: str = ""     # the member's file, when it is on the community edition's volume (`on_volume`)


@dataclass(frozen=True)
class ArchivePlan:
    """Everything an assembly needs, resolved before the response exists.

    No ORM object or DB session reaches this: the request session is closed before the generator runs,
    and a closed Session silently autobegins and holds a connection for the whole download.
    """

    filename: str            # the `Content-Disposition` name
    manifest_arcname: str
    manifest_bytes: bytes
    members: tuple[Member, ...]
    mtime: int


def _presign_get(*, bucket: str, region: str, key: str, credentials: Any, expires: int) -> str:
    if not edition_features.enabled("aws"):
        raise RuntimeError("no S3 in this edition: it reads its volume")
    from app.aws import presign

    return presign.presigned_url(
        method="GET",
        url=presign.s3_object_url(bucket=bucket, key=key, region=region),
        region=region,
        service="s3",
        credentials=credentials,
        amz_date=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        expires_seconds=expires,
    )


def _get(client: httpx.Client, url: str, *, what: str, stream: bool) -> httpx.Response:
    """One presigned GET, enforcing the header rule.

    Only `host` is signed, so any `Content-Type` or `x-amz-*` header is a 403. `Accept-Encoding:
    identity` makes `iter_raw` yield the stored object byte-for-byte.
    """
    if not edition_features.enabled("aws"):
        raise RuntimeError("no S3 in this edition: it reads its volume")
    from app.aws import presign

    request = client.build_request("GET", url, headers={"Accept-Encoding": "identity"})
    presign.reject_forbidden_headers(request.headers.keys())
    response = client.send(request, stream=stream)
    if response.status_code != 200:
        status = response.status_code
        response.close()
        # THE STATUS ONLY, never the URL: it is a bearer token, and an S3 error body echoes request
        # parameters back.
        raise ArchiveAborted(f"S3 answered {status} for {what}")
    return response


def fetch_manifest(
    *, bucket: str, region: str, key: str, credentials: Any
) -> tuple[bytes, Mapping[str, Any]]:
    """The session's `manifest.json` (raw bytes and parsed document), fetched before the response.

    The raw bytes go into the archive verbatim so they still digest to `transcript_sha256`. Its own
    exception type because nothing has been sent yet, so this is still an ordinary HTTP error.
    """
    if on_volume(bucket):
        return _parsed_manifest(_read_volume_manifest(key))
    url = _presign_get(
        bucket=bucket, region=region, key=key, credentials=credentials,
        expires=PRESIGN_EXPIRES_SECONDS,
    )
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT) as client:
            response = _get(client, url, what="the manifest", stream=True)
            try:
                body = bytearray()
                for chunk in response.iter_raw():
                    body += chunk
                    if len(body) > MAX_MANIFEST_BYTES:
                        raise ArchiveAborted(
                            f"the manifest is larger than the {MAX_MANIFEST_BYTES}-byte ceiling"
                        )
            finally:
                response.close()
    except ArchiveAborted as exc:
        raise ManifestUnreadable(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise ManifestUnreadable(f"the manifest could not be read ({type(exc).__name__})") from exc
    return _parsed_manifest(bytes(body))


def _read_volume_manifest(key: str) -> bytes:
    from app import attachment_storage

    try:
        path = attachment_storage.local_path(attachment_storage.local_volume(key), key)
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ManifestUnreadable(f"the manifest is larger than the {MAX_MANIFEST_BYTES}-byte ceiling")
        return path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ManifestUnreadable(f"the manifest could not be read ({type(exc).__name__})") from exc


def _parsed_manifest(body: bytes) -> tuple[bytes, Mapping[str, Any]]:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestUnreadable(f"the manifest could not be read ({type(exc).__name__})") from exc
    if not isinstance(document, dict) or not isinstance(document.get("members"), list):
        raise ManifestUnreadable("the manifest carries no `members` array")
    return body, document


def plan_archive(
    *,
    agent_session_id: str,
    started_at: datetime,
    manifest_bytes: bytes,
    manifest: Mapping[str, Any],
    bucket: str,
    region: str,
    credentials: Any,
) -> ArchivePlan:
    """Resolve every member to a key and a presigned URL. Pure: no DB, no network, no event loop.

    Keys are RE-DERIVED from the row's own `started_at` and the member's allowlisted name — the
    manifest never carries a key, so a node cannot name one.
    """
    if not transcripts.is_usable_session_id(agent_session_id):
        raise ManifestUnreadable("the session id is not usable as an object-key segment")
    root = f"session-{agent_session_id}"
    volume = on_volume(bucket)
    entries = manifest.get("members", ())
    # The upload ticket's bounds, re-applied: `bytes_raw` is the node's and bounds what we stream.
    if len(entries) > transcripts.MEMBER_LIMIT:
        raise ManifestUnreadable(
            f"the manifest names {len(entries)} members, more than the {transcripts.MEMBER_LIMIT} "
            "the control plane issues keys for"
        )
    members: list[Member] = []
    named: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ManifestUnreadable("a manifest member is not an object")
        name = entry.get("name")
        if not isinstance(name, str):
            raise ManifestUnreadable("a manifest member carries no name")
        # The allowlist runs first, so no message echoes a name the CP does not issue keys for.
        try:
            key = transcripts.member_key(
                session_id=agent_session_id, started_at=started_at, name=name
            )
        except transcripts.KeyRefused as exc:
            raise ManifestUnreadable(str(exc)) from exc
        # The report path does not dedupe names, so a repeat would mean repeated full fetches.
        if name in named:
            raise ManifestUnreadable(f"member {name!r} is named twice")
        named.add(name)
        try:
            bytes_raw = int(entry["bytes_raw"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            # `json` maps `1e400` to inf, and `int(inf)` raises OverflowError.
            raise ManifestUnreadable(f"member {name!r} carries no usable `bytes_raw`") from exc
        if not 0 <= bytes_raw <= transcripts.MAX_MEMBER_BYTES:
            raise ManifestUnreadable(
                f"member {name!r} claims {bytes_raw} bytes, outside the "
                f"0..{transcripts.MAX_MEMBER_BYTES} the control plane issues keys for"
            )
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not digest:
            raise ManifestUnreadable(f"member {name!r} carries no `sha256`")
        members.append(
            Member(
                name=name,
                arcname=f"{root}/{_unpacked_name(name)}",
                url="" if volume else _presign_get(
                    bucket=bucket, region=region, key=key, credentials=credentials,
                    expires=PRESIGN_EXPIRES_SECONDS,
                ),
                bytes_raw=bytes_raw,
                sha256=digest.lower(),
                path=str(volume_path(key)) if volume else "",
            )
        )
    return ArchivePlan(
        filename=f"{root}.tar.gz",
        manifest_arcname=f"{root}/{_MANIFEST_ARCNAME}",
        manifest_bytes=manifest_bytes,
        members=tuple(members),
        mtime=int(as_utc(started_at).timestamp()),
    )


def storage() -> Optional[tuple[str, str, Any]]:
    """`(region, configured bucket, credentials)` when transcript storage is usable here, else None.
    `resolve_credentials()` never raises, so an outage is a value, not an exception. Imported at call time so
    a test's patch of either source is seen."""
    from app.config import session_transcript_config

    config = session_transcript_config()
    if config is None:
        return None
    if not edition_features.enabled("aws"):
        # Unreachable: the config above is always None without `aws`.
        return None
    from app.aws.container_credentials import resolve_credentials

    credentials = resolve_credentials()
    return None if credentials is None else (*config, credentials)


def on_volume(bucket: Optional[str]) -> bool:
    """Whether session objects in `bucket` live on the community edition's volume (`attachment_storage.local_volume`):
    always in an edition without `aws` (it has no S3; a row's recorded bucket, `LOCAL_BUCKET` or one from an older
    release, is read from the volume), never in the cloud edition, whatever the credentials say."""
    from app import edition_features

    return not edition_features.enabled("aws")


def available() -> bool:
    """Whether this control plane can store a session's objects at all: a bucket, or the community edition's volume."""
    return storage() is not None or on_volume(None)


def stored_at(bucket: Optional[str]) -> Optional[tuple[str, str, Any]]:
    """`located`, with the community edition's volume as `("", LOCAL_BUCKET, None)`: no region, no credentials."""
    if on_volume(bucket):
        from app.attachment_storage import LOCAL_BUCKET

        return "", LOCAL_BUCKET, None
    return located(bucket)


def volume_path(key: str) -> Any:
    """`key`'s file on the community edition's volume. Raises ValueError for a key that could name anything outside it."""
    from app import attachment_storage

    return attachment_storage.local_path(attachment_storage.local_volume(key), key)


def write_volume(key: str, body: bytes) -> None:
    """Store one CP-authored session object (an events chunk, the manifest) on the community edition's volume."""
    from app import attachment_storage

    attachment_storage.write_local(attachment_storage.local_volume(key), key, body)


def located(bucket: Optional[str]) -> Optional[tuple[str, str, Any]]:
    """`storage()` for an object in `bucket` — a row's stored bucket, or the one `data_residency.bucket_for_org`
    picked for a write: `(region, bucket, credentials)`, the region being that bucket's own data region
    (`data_residency.region_of_bucket`). Blank or the configured bucket = `storage()` unchanged (a legacy NULL
    row, an org that never picked). None when storage is not configured here."""
    found = storage()
    if found is None or not bucket or bucket == found[1]:
        return found
    from app import data_residency  # Local: it reads operator settings, which this module must not pull in.

    return data_residency.region_of_bucket(bucket), bucket, found[2]


def org_bucket(session: Any, org_id: Any) -> Optional[str]:
    """The bucket a NEW transcript, events chunk or attachment of `org_id` goes to, for `located`: its data region's
    (`data_residency.bucket_for_region`), None — the configured bucket — when it picked none."""
    from app import data_residency

    chosen = data_residency.org_region(session, org_id)
    return data_residency.bucket_for_region(chosen) if chosen else None


def pin_bucket(session: Any, row: Any) -> Optional[str]:
    """The bucket session `row`'s objects go to, fixed on the row at its FIRST write (an events chunk or an upload
    ticket) and never moved after: the org's data region's then (`org_bucket`), else the configured bucket. Before
    any object exists, so a region change (`data_residency.blockers` counts the row) or offboarding and the
    retention sweep (they read the row's bucket) never miss bytes an unfinished upload left. A fenced UPDATE, so
    two first writers agree, and committed with the caller's transaction; None when storage is not configured. The
    community edition pins its volume (`on_volume`)."""
    if row.transcript_bucket:
        return row.transcript_bucket
    found = storage()
    if found is None and not on_volume(None):
        return None
    from sqlalchemy import select, update

    from app.attachment_storage import LOCAL_BUCKET

    from app.models.agent_sessions import AgentSession

    session.execute(
        update(AgentSession)
        .where(AgentSession.id == row.id, AgentSession.transcript_bucket.is_(None))
        .values(transcript_bucket=(org_bucket(session, row.org_id) or found[1]) if found else LOCAL_BUCKET)
    )
    row.transcript_bucket = session.scalar(select(AgentSession.transcript_bucket).where(AgentSession.id == row.id))
    return row.transcript_bucket


def fetch_row_manifest(row: Any) -> tuple[bytes, Mapping[str, Any], str, str, Any]:
    """Session `row`'s manifest (raw, parsed) plus the `bucket, region, credentials` it was read with. The row's
    bucket (and its region, `located`) and stored manifest key win; the key is derived only when none was stored.
    Raises `ManifestUnreadable`, also when storage is not configured here."""
    found = stored_at(row.transcript_bucket)
    if found is None:
        raise ManifestUnreadable("transcript storage is not configured here")
    region, bucket, credentials = found
    manifest_bytes, manifest = fetch_manifest(
        bucket=bucket, region=region, credentials=credentials,
        key=row.transcript_manifest_key
        or transcripts.manifest_key(session_id=row.agent_session_id, started_at=row.started_at),
    )
    return manifest_bytes, manifest, bucket, region, credentials


def open_main(row: Any) -> tuple[Mapping[str, Any], Optional[Member]]:
    """Session `row`'s parsed manifest and its main transcript member (None when it has none). Raises
    `ManifestUnreadable`."""
    manifest_bytes, manifest, bucket, region, credentials = fetch_row_manifest(row)
    plan = plan_archive(
        agent_session_id=row.agent_session_id, started_at=row.started_at, manifest_bytes=manifest_bytes,
        manifest=manifest, bucket=bucket, region=region, credentials=credentials,
    )
    return manifest, next((m for m in plan.members if m.name == transcripts.MAIN_TRANSCRIPT_MEMBER), None)


def _unpacked_name(name: str) -> str:
    return name[: -len(_STORED_SUFFIX)] if name.endswith(_STORED_SUFFIX) else name


class _Sink:
    """The archive's write end: gzip writes here, the generator drains it and yields what appeared.

    `drain` first pushes the attached `GzipFile`'s own write buffer into the compressor: it is
    `4 * io.DEFAULT_BUFFER_SIZE`, 512 KiB since 3.14 raised that size, so without the push a whole
    transcript sits in it and nothing streams. `Z_NO_FLUSH` moves the bytes without a deflate flush,
    so compression is unchanged.
    """

    __slots__ = ("_chunks", "_gz")

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._gz: Optional[gzip.GzipFile] = None

    def attach(self, gz: gzip.GzipFile) -> None:
        self._gz = gz

    def write(self, data: bytes) -> int:
        if data:
            self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def drain(self) -> list[bytes]:
        if self._gz is not None and not self._gz.closed:
            self._gz.flush(zlib.Z_NO_FLUSH)
        chunks, self._chunks = self._chunks, []
        return chunks


def _tarinfo(*, arcname: str, size: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=arcname)
    info.size = size
    info.mtime = mtime
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _emit(tar: tarfile.TarFile, sink: _Sink, info: tarfile.TarInfo,
          chunks: Iterator[bytes], deadline: _Deadline) -> Iterator[bytes]:
    """One member: header, data, padding; `TarFile.addfile`'s body unrolled around the yield.

    `addfile(info, fileobj)` copies the whole member in one call, buffering it; splitting makes this a
    stream. The header is written here because 3.13 made `addfile(info)` without a fileobj a
    `ValueError` for a non-empty file. `tar.offset` must stay exact: `TarFile.close` pads from it.
    """
    header = info.tobuf(tar.format, tar.encoding, tar.errors)
    tar.fileobj.write(header)
    tar.offset += len(header)
    yield from sink.drain()
    written = 0
    # `closing` so an abort releases the open S3 response at the raise.
    with closing(chunks):
        for chunk in chunks:
            deadline.check()
            written += len(chunk)
            if written > info.size:
                raise ArchiveAborted(
                    f"member {info.name!r} is longer than its manifest size ({info.size} bytes)"
                )
            tar.fileobj.write(chunk)
            yield from sink.drain()
    if written != info.size:
        raise ArchiveAborted(
            f"member {info.name!r} ended after {written} of {info.size} manifest bytes"
        )
    blocks, remainder = divmod(info.size, tarfile.BLOCKSIZE)
    if remainder:
        tar.fileobj.write(tarfile.NUL * (tarfile.BLOCKSIZE - remainder))
        blocks += 1
    tar.offset += blocks * tarfile.BLOCKSIZE
    yield from sink.drain()


def _transport_reads(response: httpx.Response, member: Member) -> Iterator[bytes]:
    """`iter_raw`, with a mid-stream transport failure wrapped as the one abort type callers match.

    A `ReadTimeout` or a `RemoteProtocolError` aborts the archive exactly as a bad digest does; the
    only difference should be the message, not the type a caller has to catch.
    """
    try:
        yield from response.iter_raw()
    except httpx.HTTPError as exc:
        raise ArchiveAborted(
            f"member {member.name!r} stopped mid-read ({type(exc).__name__})"
        ) from exc


def _member_raw(client: httpx.Client, member: Member) -> Generator[bytes, None, None]:
    """The member's stored (gzipped) bytes: its file on the volume, else the presigned GET's."""
    if member.path:
        try:
            with open(member.path, "rb") as handle:
                yield from iter(lambda: handle.read(64 * 1024), b"")
        except OSError as exc:
            raise ArchiveAborted(f"member {member.name!r} could not be read ({type(exc).__name__})") from exc
        return
    response = _get(client, member.url, what=f"member {member.name!r}", stream=True)
    try:
        yield from _transport_reads(response, member)
    finally:
        response.close()


def _member_chunks(client: httpx.Client, member: Member) -> Iterator[bytes]:
    """The member's decompressed bytes, integrity-checked as they pass.

    A transport check: `sha256` is the node's own number over pre-gzip bytes, so it catches a corrupted
    S3 read, not a lying node. The abort still precedes the trailer, which is what the client detects.
    """
    digest = hashlib.sha256()
    inflate = zlib.decompressobj(_GZIP_WBITS)
    seen = 0
    # `closing`: an abort mid-member releases the open S3 response or file at the raise.
    with closing(_member_raw(client, member)) as raws:
        for raw in raws:
            # Bounded inflate: `unconsumed_tail` must be fed to the next call. Terminates because each
            # iteration consumes input.
            pending = raw
            while pending:
                try:
                    data = inflate.decompress(pending, _MAX_INFLATE_CHUNK)
                except zlib.error as exc:
                    raise ArchiveAborted(
                        f"member {member.name!r} is not readable gzip: {exc}"
                    ) from exc
                pending = inflate.unconsumed_tail
                if data:
                    digest.update(data)
                    seen += len(data)
                    yield data
    try:
        tail = inflate.flush()
    except zlib.error as exc:
        raise ArchiveAborted(f"member {member.name!r} is not readable gzip: {exc}") from exc
    if tail:
        digest.update(tail)
        seen += len(tail)
        yield tail
    if not inflate.eof:
        raise ArchiveAborted(f"member {member.name!r} ended mid-gzip-stream")
    if inflate.unused_data:
        raise ArchiveAborted(f"member {member.name!r} carries bytes past the end of its gzip stream")
    # Defence in depth: `_emit` checks the same numbers, but member integrity is decidable here alone.
    if seen != member.bytes_raw:
        raise ArchiveAborted(
            f"member {member.name!r} is {seen} bytes, the manifest says {member.bytes_raw}"
        )
    actual = digest.hexdigest()
    if actual != member.sha256:
        raise ArchiveAborted(f"member {member.name!r} failed its sha256 (transport integrity)")


def iter_member(member: Member) -> Iterator[bytes]:
    """One member's decompressed bytes, integrity-checked as they pass, outside any archive — for a CP reader
    of a transcript (`app/project_memory/distill.py`). Raises `ArchiveAborted` as `_member_chunks` does."""
    with httpx.Client(timeout=FETCH_TIMEOUT) as client:
        yield from _member_chunks(client, member)


def _discard(tar: tarfile.TarFile, gz: gzip.GzipFile) -> None:
    """Retire both writers after an abort, having written no trailer.

    Marking the tar stream closed stops `_Stream.__del__` flushing into an already-closed gzip file.
    Fails soft if that attribute moves (an ignored warning returns, never a wrong archive).
    """
    tar.fileobj.closed = True
    gz.close()


def stream_archive(plan: ArchivePlan, permit: Permit) -> Iterator[bytes]:
    """The `.tar.gz`, assembled member by member. A sync generator (see the module docstring).

    On any member failure this raises without closing the tar, so no trailer is written and the client
    sees a transport error, not a clean EOF. Hence no `Content-Length`.
    """
    try:
        deadline = _Deadline(ASSEMBLY_DEADLINE_SECONDS)
        sink = _Sink()
        gz = gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=GZIP_LEVEL, mtime=plan.mtime)
        sink.attach(gz)
        tar = tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT)
        try:
            info = _tarinfo(
                arcname=plan.manifest_arcname, size=len(plan.manifest_bytes), mtime=plan.mtime
            )
            tar.addfile(info, io.BytesIO(plan.manifest_bytes))
            yield from sink.drain()
            with httpx.Client(timeout=FETCH_TIMEOUT) as client:
                for member in plan.members:
                    deadline.check()
                    yield from _emit(
                        tar,
                        sink,
                        _tarinfo(arcname=member.arcname, size=member.bytes_raw, mtime=plan.mtime),
                        _member_chunks(client, member),
                        deadline,
                    )
            # ONLY here, and only on the clean path: these two calls ARE the trailer.
            tar.close()
            gz.close()
            yield from sink.drain()
        except BaseException:
            _discard(tar, gz)
            raise
    finally:
        # Frees a mid-stream abort; the route's `BackgroundTask` frees an early disconnect.
        permit.release()

