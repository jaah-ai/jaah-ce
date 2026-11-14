"""The account Delete's node round, off the request: the console's Delete answers once the row is gone.

`routes/accounts.py::remove_account` records every removal the account's nodes owe (`app/account_removal.py`)
in the same commit that deletes the row, then enqueues one workflow here. The workflow delivers them, node by
node, through `account_removal.settle` — the node-connect pass's own sender — in two passes: every token-share
leaf first, since a leaf is every attached node's way back to the login, then everything else. A node that is offline or fails keeps owing and gets
it again on its next connect, so a lost or failed enqueue loses nothing: it only waits for that connect.

Only identifiers travel (account id, node ids); no secret comes near this module."""
from __future__ import annotations

import logging
from typing import Optional

from dbos import DBOS

from app.durable import runtime
from app.durable.runtime import enqueue_or_warn

log = logging.getLogger("account-remove")

#: Deletes delivering at once; each step is one node's round (up to `account_removal._TIMEOUT_SECONDS` per verb).
QUEUE = runtime.declare_queue("account-remove", concurrency=4, polling_interval_sec=runtime.INTERACTIVE_POLL_SEC)


def workflow_id(account_id: str) -> str:
    return f"account-remove:{account_id}"


@DBOS.step()
def settle_node(node_id: str, verb: Optional[str] = None) -> None:
    """Send `node_id` every removal it owes (only `verb`'s, when given). Never raises (`account_removal.settle`)."""
    from app import account_removal

    account_removal.settle(node_id, verb)


@DBOS.workflow()
def deliver_removals(account_id: str, share_node_ids: list[str], node_ids: list[str]) -> None:
    """`account-remove:{account}` (module docstring): the share leaves on `share_node_ids`, then every removal
    `node_ids` owe."""
    from app.tenancy.offboard import SHARED_DELETE_VERB

    for node_id in share_node_ids:
        settle_node(node_id, SHARED_DELETE_VERB)
    for node_id in node_ids:
        settle_node(node_id)


def enqueue_removal(account_id: str, share_node_ids: list[str], node_ids: list[str]) -> bool:
    """Hand one account Delete's node round to the durable queue, after the delete commits. Never raises."""
    if not node_ids:
        return True
    return enqueue_or_warn(
        QUEUE, workflow_id(account_id), deliver_removals, account_id, share_node_ids, node_ids, log=log,
        failure=f"account {account_id}: could not enqueue its removal on {len(node_ids)} node(s)",
        consequence="each node still owes it and carries it out on its next connect",
    )
