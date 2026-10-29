"""The control plane's apt channel — the fleet's signed apt repository, re-served to a node that holds
no AWS credential.

A CP-created node has no IAM instance profile to read the private bucket, and neither handing out AWS
keys nor a public bucket is acceptable, so the CP redirects a node-token holder to a short-lived
presigned URL for the one object asked for. This gives confidentiality and access control only;
integrity and authenticity rest on the GPG `Signed-By` keyring, so never drop the signing.

Auth is `routes/agent/auth.py::_check_token`, the one door onto `/api/agent/*`, preceded by a Basic-to-header
translation because apt cannot send a custom header. `object_path` becomes an S3 key, so `_object_key`
is an allowlist and every refusal is the same 404. `APT_PATH` and the pubkey are imported from
`app/node_bootstrap.py` so the sources file and this route cannot drift.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from app import edition_features, node_credentials
from app.operator_settings import reads as settings_reads
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.config import get_settings
from app.node_bootstrap import APT_PATH, APT_PUBKEY_PATH
# The only authenticator on `/api/agent/*` (`@infra` permits and `PUBLIC_PREFIXES` skips the session
# middleware); `test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check` enforces it.
from app.routes.agent.auth import _check_token


# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator to
# prove it is refused by the public ALB, so the full path is spelled on the route.
router = APIRouter(tags=["agent-apt"])

# The operator knob naming the bucket; the 503 below names this string to the operator.
APT_BUCKET_SETTING = "cloud_node_apt_bucket"

# The one object at the bucket root: the signing key the bootstrap `curl`s before apt runs. `+ 1` skips
# the separating `/`.
PUBKEY_OBJECT = APT_PUBKEY_PATH[len(APT_PATH) + 1:]

# Everything else is under this prefix — the boundary, since the bucket holds objects never to serve.
APT_KEY_PREFIX = "apt/"

# Short: a bearer credential sent to a machine we do not administer; it need survive only one redirect
# plus the fetch, with slack for a slow link and some clock skew.
APT_URL_EXPIRES_SECONDS = 90

# apt matches `auth.conf.d` on URI, not realm; this is for a human reading the 401.
_BASIC_REALM = 'Basic realm="jaah-apt"'

# Exactly the characters an apt repository uses; everything else (backslash, NUL, control, whitespace,
# quotes, `%`) is refused by this one allowlist. `+` is a Debian version's (`jaah-python_3.14.7+20260929_amd64.deb`).
_KEY_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~/-+"
)
# The one encoding apt itself sends: it percent-encodes a pool filename's `+` (`%2b`).
_APT_PLUS = re.compile(r"%2[bB]")

# Real paths are ~5 segments / ~50 bytes; both bounds sit far above that.
_MAX_PATH_LENGTH = 512
_MAX_SEGMENTS = 16


def _refuse_unauthenticated() -> NoReturn:
    """The 401 apt has to see before it will send a credential at all.

    apt sends credentials only after a 401 carrying `WWW-Authenticate`; a 403 would end `apt-get
    update` with no retry.
    """
    raise HTTPException(
        status_code=401,
        detail="a node token is required, as an X-Agent-Token header or an HTTP Basic password",
        headers={"WWW-Authenticate": _BASIC_REALM},
    )


def _basic_credentials_as_agent_token(request: Request) -> None:
    """Translate HTTP Basic into the header `_check_token` reads, before it runs.

    Done here, not in `_check_token`, so Basic is accepted on this route alone. The username is ignored;
    the password is the node token. Every malformed shape is the same 401, never a 500, since this runs
    on unauthenticated input. Presenting nothing is refused here, so `ALLOW_INSECURE_AGENT` does not
    apply to this route.
    """
    if request.headers.get("x-agent-token"):
        return  # an explicit header always wins; never override what the caller stated outright
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        _refuse_unauthenticated()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        _refuse_unauthenticated()
    _username, separator, password = decoded.partition(":")
    if not separator or not password:
        _refuse_unauthenticated()
    # Starlette memoises headers on the Request as `_headers`; drop it or `_check_token` reads the old view.
    request.scope["headers"] = [
        (k, v) for (k, v) in request.scope["headers"] if k != b"x-agent-token"
    ] + [(b"x-agent-token", password.encode("ascii"))]
    request.__dict__.pop("_headers", None)


def _raw_path(request: Request) -> str:
    """The request target as it arrived on the wire, before the server decoded it.

    The routed `object_path` is already percent-decoded, so `%2e%2e` and `..` look alike. Without
    `raw_path` it falls back to the decoded path, losing only the refusal of redundant encodings.
    """
    raw = request.scope.get("raw_path")
    if isinstance(raw, bytes):
        # `raw_path` carries the query string on some servers; the key never does.
        return raw.split(b"?", 1)[0].decode("latin-1")
    return request.url.path


def _object_key(object_path: str, raw_path: str) -> str | None:
    """The S3 key `object_path` names, or `None` if it may not be served; the caller 404s either way.

    One refusal value, so malformed and missing are indistinguishable (no validity oracle). The only
    encoding admitted is apt's own `%2b` for `+`; any other `%` in the raw path is refused, and a double
    encoding (`%252b`) leaves a `%` that is refused here or decodes to one the charset check refuses.
    """
    if "%" in _APT_PLUS.sub("", raw_path):
        return None
    if not object_path or len(object_path) > _MAX_PATH_LENGTH:
        return None
    if object_path.startswith("/"):
        return None
    if not _KEY_CHARACTERS.issuperset(object_path):
        return None
    segments = object_path.split("/")
    if len(segments) > _MAX_SEGMENTS:
        return None
    if any(segment in ("", ".", "..") for segment in segments):
        # `""` covers a trailing slash and `//`; encoded `.`/`..` already died at the `%` gate.
        return None
    # Two shapes only: the root signing key, or repository content under `apt/`.
    if object_path == PUBKEY_OBJECT:
        return PUBKEY_OBJECT
    return f"{APT_KEY_PREFIX}{object_path}"


@router.get(f"{APT_PATH}/{{object_path:path}}", status_code=307)
@infra(Tenancy.OPERATOR)
def serve_apt_object(
    object_path: str,
    request: Request,
    # Order is the contract: the translation runs first so `_check_token` sees the header.
    _basic: None = Depends(_basic_credentials_as_agent_token),
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> RedirectResponse:
    """Redirect one authenticated node to a short-lived presigned GET for one repository object.

    A redirect, not a proxy, so the CP never streams package bytes; apt does not forward its Basic
    credentials to the redirect target.
    """
    key = _object_key(object_path, _raw_path(request))
    if key is None:
        # Before any config read, so a hostile path gets the same 404 whether or not a channel exists.
        raise HTTPException(status_code=404, detail="no such object")

    return presigned_redirect(key, channel="apt", caller=caller)


def presigned_redirect(
    key: str, *, channel: str, caller: node_credentials.AgentCaller
) -> RedirectResponse:
    """A 307 to a 90-second presigned GET for `key` in the apt bucket, or a 503 naming what is missing.

    The single home for the bucket/region/credential resolution and the signing, shared by every
    channel re-serving this bucket (`apt`, `dist` in `routes/agent_dist.py`). `key` must already be
    allowlisted by the caller; `channel` is only the word the 503s and the log line name. Neither the
    token nor the `Location` (a bearer credential) is ever logged — only the caller and key.
    """
    bucket = settings_reads.get_str(APT_BUCKET_SETTING)
    if not bucket:
        raise HTTPException(
            status_code=503,
            detail=(
                f"the {channel} channel is not configured: set the {APT_BUCKET_SETTING!r} setting "
                "(Settings > Infrastructure > Cloud) to the bucket holding the signed jaah repository"
            ),
        )
    region = get_settings().aws_region
    if not region:
        raise HTTPException(
            status_code=503,
            detail=(
                f"the {channel} channel is not configured: AWS_REGION is empty, so the control plane "
                "cannot name the bucket's endpoint"
            ),
        )
    if not edition_features.enabled("apt_self_upgrade"):
        raise HTTPException(status_code=503, detail=f"the {channel} channel is not in this edition")
    from app.aws import container_credentials, presign

    credentials = container_credentials.resolve_credentials()
    if credentials is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"the control plane holds no AWS credentials to sign an {channel} URL with — attach its "
                "task role, or set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY on a local stack"
            ),
        )

    try:
        url = presign.presigned_url(
            method="GET",
            # Constructed, never accepted: `presigned_url` signs whatever host it is given.
            url=presign.s3_object_url(bucket=bucket, key=key, region=region),
            region=region,
            service="s3",
            credentials=credentials,
            amz_date=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            expires_seconds=APT_URL_EXPIRES_SECONDS,
        )
    except ValueError as exc:
        # An unsignable operator bucket or region: a 503, not a 500; `presign` names the parameter,
        # never its value.
        logging.getLogger(f"agent-{channel}").warning("agent-%s: cannot sign an %s URL (%s)", channel, channel, exc)
        raise HTTPException(
            status_code=503,
            detail=(
                f"the {channel} channel is misconfigured and no URL could be signed: {exc}. Check the "
                f"{APT_BUCKET_SETTING!r} setting and AWS_REGION"
            ),
        ) from exc

    logging.getLogger(f"agent-{channel}").info(
        "agent-%s: serving %s to node=%s arm=%s", channel, key, caller.node_id, caller.arm
    )
    return RedirectResponse(
        url=url,
        status_code=307,
        # The `Location` is a credential with a 90-second life; no cache, no proxy store, ever.
        headers={"Cache-Control": "no-store"},
    )
