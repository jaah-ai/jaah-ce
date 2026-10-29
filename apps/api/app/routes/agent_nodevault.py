"""The node side of NodeVault on the agent surface: a node registers its identity key.

Each node daemon mints a long-lived RSA-4096 identity key (NIK) and posts its PUBLIC half here until
acknowledged. The control plane computes the fingerprint itself (`hex(sha256(SPKI DER))`, the same
derivation as a key hash) and never takes one from the node. The key is set ONCE: the same key re-posted
is an idempotent 200, a different key a 409 until an admin resets the node's identity — a wiped keystore,
or something posing as the node, never swaps it silently. `capable` (the daemon's crypto probe) may change
on every post. A node that is not capable (every Windows node, and a macOS or Linux node whose runners share
the daemon's uid) mints no key and posts `public_key: null`: that records `capable=false` and nothing else,
where it used to be a 422 retried forever in the node's log. After the commit, `nodevault_grants.ensure_grants` enrols
the node in its org's live keys,
which is how a brand-new node gets the key with no task needed.

Authentication is `routes/agent/auth.py::_check_token` and nothing else; the node is the credential's (`caller_node_id`).
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictBool, StrictStr
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import node_credentials, nodevault_grants
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.credential_keys import nodevault_key_hash, parse_nodevault_public_key
from app.db import get_session
from app.entity_shape import get_or_404, StrictModel
from app.models.nodes import Node
# The one door onto `/api/agent/*`; `test_authz_policy.py` fails any route under the prefix without it.
from app.routes.agent.auth import _check_token, caller_node_id

log = logging.getLogger("agent-nodevault")

# No `prefix=`: the public/private ALB split test reads each `@infra` route's path off its decorator.
router = APIRouter(tags=["agent-nodevault"])

IDENTITY_TAKEN = "identity key already registered"


class NodeVaultIdentityIn(StrictModel):
    # Base64 SPKI DER of the node's RSA-4096 identity key: public, so echoing it in a 422 leaks nothing. None from
    # a node that is not capable: it holds no key.
    public_key: Optional[StrictStr]
    capable: StrictBool
    # Only for a caller with no derived node (the CP loopback); a node's own credential decides otherwise.
    node_id: Optional[str] = None


class NodeVaultIdentityOut(BaseModel):
    # None when the node posted no key.
    fingerprint: Optional[str]


@router.post("/api/agent/nodevault/identity", response_model=NodeVaultIdentityOut)
@infra(Tenancy.ORG_SCOPED)
def register_nodevault_identity(
    body: NodeVaultIdentityIn,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> NodeVaultIdentityOut:
    """Register (or re-confirm) the CALLING node's identity key and its `capable` flag."""
    node_id = caller_node_id(session, caller, body.node_id or "")
    if body.public_key is None:
        return _record_incapable(session, node_id, capable=body.capable)
    try:
        spki = parse_nodevault_public_key(body.public_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    fingerprint = nodevault_key_hash(spki)
    node = get_or_404(session, Node, node_id, detail="node not found", lock=True)
    if node.nodevault_identity_fp is not None and node.nodevault_identity_fp != fingerprint:
        raise HTTPException(status_code=409, detail=IDENTITY_TAKEN)
    if node.nodevault_identity_fp is None:
        node.nodevault_identity_key = base64.b64encode(spki).decode()
        node.nodevault_identity_fp = fingerprint
        node.nodevault_identity_at = func.now()
    node.nodevault_capable = body.capable
    node.nodevault_capable_at = func.now()
    session.commit()
    try:
        nodevault_grants.ensure_grants(session, node_id)
        session.commit()
    except Exception:  # noqa: BLE001 — the identity is registered; `hello` re-runs enrolment
        session.rollback()
        log.warning("NodeVault enrolment of node %s failed after its identity registered", node_id, exc_info=True)
    return NodeVaultIdentityOut(fingerprint=fingerprint)


def _record_incapable(session: Session, node_id, *, capable: bool) -> NodeVaultIdentityOut:
    """A keyless post: only a node that says it is not capable may make one; its flag is all that changes (a key
    registered earlier stays, so a later capable post still meets the 409 rule)."""
    if capable:
        raise HTTPException(status_code=422, detail="a capable node must post its public_key")
    node = get_or_404(session, Node, node_id, detail="node not found", lock=True)
    node.nodevault_capable = False
    node.nodevault_capable_at = func.now()
    session.commit()
    return NodeVaultIdentityOut(fingerprint=None)
