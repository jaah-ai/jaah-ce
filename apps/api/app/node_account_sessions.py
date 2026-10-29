"""What a node's deletion does to the accounts signed in on it: their sessions live in its config dir and its
`account_observations` cascade, so each such account is stamped `Account.node_removed_at` for the Accounts page to
say so. Both node deletes call it: `routes/nodes/delete.py` and a node group's scale-in
(`node_group_reconcile.py`)."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update

from app.account_fold import NODE_STATUS_NO_CREDENTIALS
from app.models.credentials import Account, AccountObservation


def mark_sessions_lost(session, node_id: UUID, org_id: UUID) -> None:
    """Stamp the org's accounts `node_id` held a session for (an observation not saying `no_credentials`), in the
    caller's transaction and before the node row goes. Org-pinned in the statement: the write seam filters no UPDATE."""
    session.execute(
        update(Account)
        .where(
            Account.org_id == org_id,
            Account.id.in_(
                select(AccountObservation.account_id).where(
                    AccountObservation.node_id == node_id,
                    AccountObservation.org_id == org_id,
                    AccountObservation.extra["status"].astext.is_distinct_from(NODE_STATUS_NO_CREDENTIALS),
                )
            ),
        )
        .values(node_removed_at=func.now())
        .execution_options(synchronize_session=False)
    )
