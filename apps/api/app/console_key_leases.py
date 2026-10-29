"""Console key leases — the node-bound, expiring authorization a console on an API-key account runs on
(Architecture §3.5a).

A fleet runner's key is authorized by its slot's lease (`worktrees.leased_account_id`); a console tab has
no slot lease, so the CP writes one of these at the pick instead, and `routes/agent/task_secrets.py::get_account_api_key`
admits the account for the leasing node until `expires_at`. Every other conjunct of that route still binds,
so disabling or deleting the account ends access at once. The residual (node-granular, 24 h, not revoked on
tab close, audit stops at user + node) is accepted by design.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app.models.credentials import ConsoleApiKeyLease

log = logging.getLogger(__name__)

#: How long one console create's key authorization lives. A direct grant lives up to the same 24 h
#: (`app/console_direct.py`), so a tab never outlives its lease by more than a reopen.
LEASE_TTL = timedelta(hours=24)


def grant(
    session: Session, *, node_id: UUID, account_id: UUID, org_id: UUID, created_by: UUID,
    now: Optional[datetime] = None,
) -> ConsoleApiKeyLease:
    """Write one lease and flush it; the caller commits. A reopen calls this again — a fresh row.

    Expiry is stamped on the database's clock, the one `live_account_ids` and the sweep compare against;
    `now` (a test's) overrides it."""
    lease = ConsoleApiKeyLease(
        node_id=node_id, account_id=account_id, org_id=org_id, created_by=created_by,
        expires_at=(now + LEASE_TTL) if now is not None else func.now() + LEASE_TTL,
    )
    session.add(lease)
    session.flush()
    return lease


def live_account_ids(node_id: Optional[UUID]) -> Select:
    """The accounts `node_id` holds an unexpired console lease on — the key route's lease arm.

    Compared against the database's own `now()`, the clock `created_at` is stamped with."""
    return select(ConsoleApiKeyLease.account_id).where(
        ConsoleApiKeyLease.node_id == node_id,
        ConsoleApiKeyLease.expires_at > func.now(),
    )


def sweep_expired(session: Session) -> int:
    """Delete every expired lease; returns how many. Hygiene only — the arm already filters on
    `expires_at`, so an unswept row is inert. Does NOT commit (caller-transactional)."""
    removed = max(0, session.execute(
        delete(ConsoleApiKeyLease).where(ConsoleApiKeyLease.expires_at <= func.now())
    ).rowcount or 0)
    if removed:
        log.info("console key leases: removed %d expired lease(s)", removed)
    return removed
