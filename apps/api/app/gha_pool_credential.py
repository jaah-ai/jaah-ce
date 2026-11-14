"""The credential a GitHub runner pool mints with, wherever it lives.

A pool's credential is its node's org's row, or — only on the operator plane — the operator's own personal
`github` credential, which lives in the operator org while the node may be a tenant's
(`routes/gha_runners.py::_validated_credential` is the one rule that admits it). An org-bound reader (the
node's agent routes, the durable probe bound to the project's org) cannot see that row, so every reader
resolves it here: first through its own session, then on the operator plane, where only a personal
`github` row is served. The GitHub token never leaves the control plane; a reader gets the row's name and
a callable that returns its token.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import git_access
from app.db import BridgedSession
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.node_groups import GhaRunnerPool
from app.tenancy import seam
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS


@dataclass(frozen=True)
class PoolCredential:
    """A resolved pool credential: its id and name for messages, `token()` for each GitHub call."""

    id: UUID
    name: str
    token: Callable[[], str]
    # Why this credential may not mint now (a lent one whose repository moved), or None.
    refusal: Optional[str] = None


def operators_own_clause():
    """The rows an operator may lend a tenant node's pool: their personal `github` credentials on the
    operator plane, while their owner is still an active superadmin. The owner is the route's to check;
    this narrows the plane, the kind and the lender's standing."""
    lender_is_operator = (
        select(AdminUser.id)
        .where(
            AdminUser.id == Credential.owner_id, AdminUser.is_superadmin.is_(True),
            AdminUser.disabled_at.is_(None), AdminUser.org_id.in_(OPERATOR_PLANE_ORG_IDS),
        )
        .exists()
    )
    return (
        Credential.org_id.in_(OPERATOR_PLANE_ORG_IDS),
        Credential.owner_id.is_not(None),
        Credential.type == "github",
        lender_is_operator,
    )


@contextmanager
def operator_plane_row(credential_id: UUID) -> Iterator[tuple[Session, Optional[Credential]]]:
    """The operator's personal `github` row `credential_id` names, on an operator-scoped session of its own.

    `detached()` first: every caller is org-bound (a node's request, the probe's `bind_from`), and
    `operator_scope()` refuses to widen a bound org."""
    with seam.detached(), seam.operator_scope(), BridgedSession() as op:
        yield op, op.scalar(
            select(Credential).where(Credential.id == credential_id, *operators_own_clause())
        )


def _operator_token(credential_id: UUID) -> str:
    with operator_plane_row(credential_id) as (op, row):
        if row is None:
            raise git_access.GitAccessError("the pool's lent credential no longer exists or its owner is no operator")
        return git_access.token_for(op, row)


def _refused(message: str) -> Callable[[], str]:
    def token() -> str:
        raise git_access.GitAccessError(message)
    return token


def resolve(session: Session, pool: GhaRunnerPool, repo_url: Optional[str]) -> Optional[PoolCredential]:
    """The pool's credential, or None when neither the caller's org nor the operator plane holds it.

    A lent one mints only for `pool.lent_repo_url`, the repository the operator saved it for: when `repo_url`
    (the project's, now) differs, its `token()` refuses, naming the remedy, so no reader mints for a repository
    the tenant pointed its project at after the save."""
    # Only the pool's own org's row is used as is: an operator's request reads every org, its own lent row too.
    row = session.scalar(
        select(Credential).where(Credential.id == pool.credential_id, Credential.org_id == pool.org_id)
    )
    if row is not None:
        return PoolCredential(row.id, row.name, lambda: git_access.token_for(session, row))
    credential_id = pool.credential_id
    # The row is named even once its lender is no active operator, so the pool still renders and deletes;
    # only minting is refused.
    with seam.detached(), seam.operator_scope(), BridgedSession() as op:
        name = op.scalar(select(Credential.name).where(
            Credential.id == credential_id, Credential.org_id.in_(OPERATOR_PLANE_ORG_IDS),
        ))
        if name is None:
            return None
        lendable = op.scalar(
            select(Credential.id).where(Credential.id == credential_id, *operators_own_clause())
        ) is not None
    if not lendable:
        why = (
            f"credential '{name}' is no longer lent: its owner is no active operator, so nothing is minted "
            "with it. Pick another credential or delete the pool."
        )
        return PoolCredential(credential_id, name, _refused(why), refusal=why)
    if not pool.lent_repo_url or pool.lent_repo_url != repo_url:
        why = (
            f"credential '{name}' was lent by the operator for {pool.lent_repo_url or 'another repository'}, "
            "and the project now names another repository, so nothing is minted with it. The operator must "
            "re-save the pool."
        )
        return PoolCredential(credential_id, name, _refused(why), refusal=why)
    return PoolCredential(credential_id, name, lambda: _operator_token(credential_id))
