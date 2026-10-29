"""The community edition's volumes, served where S3 would be: a signed PUT stores an object, a signed GET returns one
(`app/attachment_storage.py`, `local_volume`: attachments, and a session's transcript members).

The signature is the credential, as S3's presigned URL is: no session, so a node and a browser use the same
URL. 404 outside the community edition, which has no S3: every object is on its volume (`attachment_storage.target`,
`session_transcript_archive.on_volume`).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app import attachment_storage, edition
from app.authz.catalog import Tenancy
from app.authz.declarations import always

router = APIRouter(prefix=attachment_storage.CE_STORAGE_PATH, tags=["ce"])

#: What each method signs besides the key and the expiry (`attachment_storage.presign_upload`/`presign_download`).
_PUT_SIGNED = ("length",)
_GET_SIGNED = ("disposition", "content_type")
_WRONG_SIZE = "The upload's size differs from the one it was signed for."


def _volume(key: str) -> attachment_storage.Target:
    volume = attachment_storage.local_volume(key)
    if not edition.IS_CE:
        raise HTTPException(status_code=404, detail="Not Found")
    if volume.root != str(attachment_storage.SESSIONS_ROOT):
        where = attachment_storage.target()
        if where is None or not where.root:
            raise HTTPException(status_code=404, detail="Not Found")
    return volume


def _checked(request: Request, method: str, key: str, names: tuple[str, ...]):
    where = _volume(key)
    if not attachment_storage.local_url_is_valid(method, key, dict(request.query_params), names):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired.")
    try:
        return where, attachment_storage.local_path(where, key)
    except ValueError:
        raise HTTPException(status_code=403, detail="This link is invalid or has expired.") from None


@router.put("/{key:path}")
@always(Tenancy.BOOTSTRAP)
async def put_object(key: str, request: Request) -> Response:
    """Store the body at `key` — exactly the signed length, as S3 refuses any other (`presign.SIGNABLE_CONTENT_LENGTH`).
    Streamed to disk, so an object of any allowed size never sits in the single API process's memory."""
    where, _ = _checked(request, "PUT", key, _PUT_SIGNED)
    length = request.query_params.get("length", "")
    if not length.isdigit() or request.headers.get("content-length", "") != length:
        raise HTTPException(status_code=403, detail=_WRONG_SIZE)
    upload = attachment_storage.LocalUpload(where, key)
    try:
        async for chunk in request.stream():
            upload.write(chunk)
            if upload.size > int(length):
                raise HTTPException(status_code=403, detail=_WRONG_SIZE)
        if upload.size != int(length):
            raise HTTPException(status_code=403, detail=_WRONG_SIZE)
    except BaseException:
        upload.abort()
        raise
    return Response(status_code=200, headers={"ETag": upload.commit()})


@router.get("/{key:path}")
@always(Tenancy.BOOTSTRAP)
def get_object(key: str, request: Request):
    """The object at `key`, typed and named as signed. Served from the console's own origin, so it is sandboxed
    and never sniffed: agent-authored HTML must not run as the console."""
    _, path = _checked(request, "GET", key, _GET_SIGNED)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    params = request.query_params
    return FileResponse(
        path,
        media_type=params.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": params.get("disposition") or "attachment",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
