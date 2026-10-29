"""The control plane's dist channel — the macOS and Windows node bundles (tarball, zip),
re-served from the apt bucket to a node that holds no AWS credential.

Same shape and reasoning as `routes/agent_apt.py` (its docstring is the home): a node-token holder is
redirected to a short-lived presigned GET for one object; the presign itself is single-homed there
(`agent_apt.presigned_redirect`). Integrity rests on the `.sha256` sidecar, not on this channel.

A client must send `+` (legal in a version) literally: any `%` in the raw path is refused, `%2B` included.

Auth is the `X-Agent-Token` header only (installers use curl / PowerShell, which can send one), so there
is no Basic translation. `filename` becomes an S3 key, so `_dist_key` is an allowlist and every refusal is
the same 404.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from app import node_credentials
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.node_bootstrap import DIST_PATH  # the installers fetch from it; one spelling
from app.routes import agent_apt
# The only authenticator on `/api/agent/*`; see `agent_apt.py`'s import of it for the enforcing test.
from app.routes.agent.auth import _check_token

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator.
router = APIRouter(tags=["agent-dist"])

DIST_KEY_PREFIX = "dist/"
DIST_PLATFORMS = frozenset({"darwin-arm64", "windows-x64"})

# A version starts with a digit; `+` is legal in it (a local build tag), `%` never is.
_FILENAME = re.compile(
    r"jaah-[0-9][0-9A-Za-z.+-]*-(darwin-arm64\.tar\.gz|windows-x64\.zip)(\.sha256)?"
)
_ARCHIVE_PLATFORM = {"darwin-arm64.tar.gz": "darwin-arm64", "windows-x64.zip": "windows-x64"}


def _require_agent_token_header(request: Request) -> None:
    """401 when no `X-Agent-Token` is presented, ahead of `_check_token`.

    So `ALLOW_INSECURE_AGENT` (a dev bypass for a missing token) does not open this route — the same
    reason `agent_apt` refuses an anonymous caller itself.
    """
    if not request.headers.get("x-agent-token"):
        raise HTTPException(status_code=401, detail="a node token is required as an X-Agent-Token header")


def _dist_key(platform: str, filename: str, raw_path: str) -> str | None:
    """The S3 key for `platform`/`filename`, or `None` if it may not be served; the caller 404s either way."""
    if "%" in raw_path:
        return None
    if platform not in DIST_PLATFORMS:
        return None
    match = _FILENAME.fullmatch(filename)
    if match is None or _ARCHIVE_PLATFORM[match.group(1)] != platform:
        return None
    return f"{DIST_KEY_PREFIX}{platform}/{filename}"


@router.get(f"{DIST_PATH}/{{platform}}/{{filename}}", status_code=307)
@infra(Tenancy.OPERATOR)
def serve_dist_object(
    platform: str,
    filename: str,
    request: Request,
    # Order is the contract: an absent header is refused before `_check_token` could wave it through.
    _header: None = Depends(_require_agent_token_header),
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> RedirectResponse:
    """Redirect one authenticated node to a short-lived presigned GET for one installer artifact."""
    key = _dist_key(platform, filename, agent_apt._raw_path(request))
    if key is None:
        # Before any config read, so a hostile name gets the same 404 whether or not a channel exists.
        raise HTTPException(status_code=404, detail="no such object")
    return agent_apt.presigned_redirect(key, channel="dist", caller=caller)
