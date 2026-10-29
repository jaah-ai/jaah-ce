"""Whether an org may use NodeVault — THE ONE plan gate every NodeVault write calls (D6/D11).

`status(session, org)` answers three flags:
* `allowed` — the org's effective NodeVault plan term (`app/plan_terms.py::nodevault_allowed`, override → plan;
  plans default it OFF);
* `setup` — the org has a `current` NodeVault key (`nodevault_keys`), the one definition of "set up";
* `frozen` — set up but no longer allowed (the plan was turned off after setup, D11): existing NodeVault
  credentials keep working, while setup, rotation, migration, retire and new NodeVault credentials are refused.

Every `/api/nodevault/*` write route and the NodeVault branch of credential create call it; a credential FETCH
never does (D11). Nothing here writes or commits.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app import plan_terms
from app.models.credentials import NV_KEY_CURRENT, NV_OP_RUNNING, NodeVaultKey, NodeVaultOperation
from app.models.orgs import Org


@dataclass(frozen=True)
class Status:
    allowed: bool
    setup: bool
    frozen: bool


def status(session: Session, org: Org) -> Status:
    """`org`'s NodeVault plan gate: allowed by its plan, set up, and frozen (set up while not allowed)."""
    allowed = plan_terms.nodevault_allowed(session, org)
    setup = bool(session.scalar(
        select(exists().where(NodeVaultKey.org_id == org.id, NodeVaultKey.state == NV_KEY_CURRENT))
    ))
    return Status(allowed=allowed, setup=setup, frozen=setup and not allowed)


def readable(session: Session, org: Org, gate: Status) -> bool:
    """May `org` read `/api/nodevault` at all: allowed, set up, or mid-operation (a plan turned off mid-setup must
    still see — and abandon — its one running operation). The GET's gate, and the identity payload's
    `nodevault_enabled`, so the console never asks for what it would be refused."""
    return gate.allowed or gate.setup or bool(session.scalar(
        select(exists().where(NodeVaultOperation.org_id == org.id, NodeVaultOperation.state == NV_OP_RUNNING))
    ))
