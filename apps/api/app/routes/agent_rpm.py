"""The control plane's rpm channel — the fleet's signed dnf repository for Amazon Linux nodes, re-served
from the apt bucket to a node that holds no AWS credential.

Same shape and reasoning as `routes/agent_apt.py` (its docstring is the home): a node-token holder is
redirected to a short-lived presigned GET for one object, and integrity rests on the GPG signature, not on
this channel. The signing key is the apt channel's own (`APT_PUBKEY_PATH`); there is no second one here.

dnf, like apt, can send only HTTP Basic (`username=node password=<token>`), so the auth, the path
normalisation and the presign are all `agent_apt`'s, reused. On top of them `_RPM_KEY` narrows the key to
the two shapes a dnf repository has, `<arch>/repodata/<file>` and `<arch>/Packages/<name>.rpm`; every
refusal is the same 404.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from app import node_credentials
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
# A node's dnf repo file names `baseurl=<cp>{RPM_PATH}/$basearch`; the bootstrap writes it.
from app.node_bootstrap import RPM_PATH
from app.routes import agent_apt
# The only authenticator on `/api/agent/*`; see `agent_apt.py`'s import of it for the enforcing test.
from app.routes.agent.auth import _check_token

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator.
router = APIRouter(tags=["agent-rpm"])


# Under the apt prefix, so the CP task role's existing `apt/*` grant covers it.
RPM_KEY_PREFIX = f"{agent_apt.APT_KEY_PREFIX}rpm/"

_RPM_KEY = re.compile(r"apt/rpm/(x86_64|aarch64)/(repodata/[^/]+|Packages/[^/]+\.rpm)")


def _rpm_key(object_path: str, raw_path: str) -> str | None:
    """The S3 key `object_path` names, or `None` if it may not be served; the caller 404s either way.

    `agent_apt._object_key` first, for its charset, segment and percent-encoding refusals; the result can
    never be the pubkey, since it starts with `rpm/`.
    """
    key = agent_apt._object_key(f"rpm/{object_path}", raw_path)
    if key is None or _RPM_KEY.fullmatch(key) is None:
        return None
    return key


@router.get(f"{RPM_PATH}/{{object_path:path}}", status_code=307)
@infra(Tenancy.OPERATOR)
def serve_rpm_object(
    object_path: str,
    request: Request,
    # Order is the contract: the translation runs first so `_check_token` sees the header.
    _basic: None = Depends(agent_apt._basic_credentials_as_agent_token),
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> RedirectResponse:
    """Redirect one authenticated node to a short-lived presigned GET for one dnf repository object."""
    key = _rpm_key(object_path, agent_apt._raw_path(request))
    if key is None:
        # Before any config read, so a hostile path gets the same 404 whether or not a channel exists.
        raise HTTPException(status_code=404, detail="no such object")
    return agent_apt.presigned_redirect(key, channel="rpm", caller=caller)
