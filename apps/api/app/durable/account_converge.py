"""The per-node converge queue: the managed layer a shared account owes every other node, as durable work.

A shared account (`accounts.shared`: its login was observed minting on a mounted token share) is usable at
once on every node of its org reporting one (`routes/commands.py::_converge_shared_account_fleet_wide`),
but only the login node has the per-node half (managed `settings.json`, consent record, OMC, HUD wrapper, MCP config). The launch-time backstop
stays, but it only converges a node a runner is placed on, which may be never.

One workflow per (account, node): each node's converge is its own durable row with its own retry
ladder and terminal state, so an absent node is retried independently. A failed row names the
account and node. The step dispatches through `node_bus.run_on_serving_loop`, never `asyncio.run`
(a foreign loop builds a reply future nobody resolves). The dispatch body stays in
`routes/commands.py::_converge_one_node`; this module owns durability only. Adding a workflow is no
reason to bump `runtime.ACCOUNTS_WORKFLOW_VERSION` (see `task_title_queue`)."""
from __future__ import annotations

import logging
import time
from typing import Optional

from dbos import DBOS

from app.durable import runtime
from app.durable.runtime import enqueue_or_warn

# A logger of its own, so engine failures here filter apart from node failures in `cockpit-commands`.
log = logging.getLogger("account-converge")

# Nodes converging at once, across all accounts: a slow node parks one slot, not the queue. It bounds
# the control plane's worker and connection pools; each converge runs on a different host.
_CONCURRENCY = 8

# No rate limiter, deliberately: it would throttle the queue as a whole and slow one account's fan-out
# across nodes; the idle poll (`runtime.BACKGROUND_POLL_SEC`) delays only the first dequeue. There is no shared external service to protect; `_CONCURRENCY` bounds
# worker threads.

CONVERGE_QUEUE = runtime.declare_queue("account-converge", concurrency=_CONCURRENCY, polling_interval_sec=runtime.BACKGROUND_POLL_SEC)

# The retry ladder: 60s, 180s, 540s between four attempts (~13 minutes), covering a reboot or a daemon
# restart; retrying an absent node sooner only spends another `NodeNotConnected`. Bounded, so a node
# gone for days ends as a failed workflow row an operator can see. The identity below advances on a
# longer window, so the next fan-out gets a fresh ladder; the node's own trigger
# (`apps/jaahd/reconcile/accounts_probe.py::AgentsProbeCache.converge_unprovisioned`) and `fleet_dispatch.converge_account_layer`
# cover the gap.
_MAX_ATTEMPTS = 4
_RETRY_INTERVAL_SECONDS = 60.0
_RETRY_BACKOFF = 3.0
# The ladder's wall clock: three gaps of 60s, 180s and 540s.
_LADDER_SECONDS = 780.0

# The window the identity advances on. `SetWorkflowID` is an idempotent attach, so an (account, node)
# key alone would be claimed forever by its first run: a node that spent its ladder could never be
# converged again. With the clock, it is re-converged by the next fan-out within this window.
# It does not serialise a pair against itself: the bucket is fixed (`now // 900`) and the ladder plus
# attempt bodies can exceed it, so an overlapping fan-out can start a second converge. That costs one
# idempotent `provision_accounts.py --offline` round; closing it would need a lease on the pair.
_CONVERGE_WINDOW_SECONDS = 900.0


def converge_window(now: Optional[float] = None) -> int:
    """The window ordinal a converge enqueued now belongs to, with `now` injectable for tests.

    Floor division, not a per-call token: every enqueue for one pair inside one window must compute the
    same string."""
    return int((time.time() if now is None else now) // _CONVERGE_WINDOW_SECONDS)


def converge_workflow_id(slug: str, node_id: str, window: int) -> str:
    """The deterministic ID every enqueue for one (account, node) pair inside one window must agree on.

    Pure, so the identity is testable without DBOS. Keyed on account and node so replicas and recovery
    re-enqueues collide rather than start a second subprocess, and on the window so a spent ladder is not
    a permanent claim (see `_CONVERGE_WINDOW_SECONDS`). The slug is not a secret; the token never comes
    near this module.

    Inside one window a second converge is not admitted: right for a success (the layer does not expire),
    the point for a run in progress, and for a failure it costs the rest of the window, covered by the
    node's own triggers."""
    # Node id first makes the key injective: a slug containing `:` could otherwise fuse two pairs into
    # one string, silently leaving a node unconverged. A uuid cannot contain a colon, and the integer window
    # cannot either; free-form components go last. Structural rather than relying on the slug charset.
    return f"account-converge:{node_id}:{window}:{slug}"


@DBOS.step(
    retries_allowed=True,
    max_attempts=_MAX_ATTEMPTS,
    interval_seconds=_RETRY_INTERVAL_SECONDS,
    backoff_rate=_RETRY_BACKOFF,
)
def converge_on_node(slug: str, node_id: str, origin_node_id: str, actor: str) -> None:
    """One attempt at converging one node's managed layer for one shared account.

    A step so the attempt is checkpointed and retried (`retries_allowed=True`). It raises on a node that
    did not converge: `_converge_one_node` audits the real answer (timeout, `NodeNotConnected`, a non-zero
    phase) first, then raises so the workflow can retry.

    No derivable `config_dir` has two dispositions: a node that has not declared a Jaah home yet is
    audited and raised (the ladder retries it); a missing or no-longer-shared account is audited and
    returned (permanent).

    The org is derived from the origin node (the account belongs to its minting node's org), not the
    target, and bound around the call: the step runs unbound on a DBOS worker, and everything below is
    tenant-owned. `origin_node_id` is resolved inside `seam.identity_scope()`, then `bind_from` scopes the
    rest (`durable/accounts.py::ensure_row`)."""
    # Deferred: this module is imported at DBOS registration time, before the app is wired.
    from app.db import SessionLocal
    from app.models.nodes import Node
    from app.node_identity import node_id_or_none
    from app.routes.commands import _converge_one_node
    from app.tenancy import exclusion_log, seam

    # Attributed, or every read below lands as `UNATTRIBUTED` (see `app/durable/runtime.py::step_session`).
    with exclusion_log.caller("durable:account_converge.converge_on_node"):
        # A session only to derive the org; `_converge_one_node` owns its own session and transactions.
        with SessionLocal() as session:
            with seam.identity_scope():
                origin = node_id_or_none(session, origin_node_id)
            if origin is None:
                # The origin node was deleted since the login. Not retried: no later attempt can find an org to run
                # under.
                log.warning(
                    "account %r: origin node %r resolves to no row, so the converge on node %r has no "
                    "org to run under — skipped", slug, origin_node_id, node_id,
                )
                return
            with seam.bind_from(session, Node, origin):
                # Release the pool connection across the remote subprocess round (up to
                # `_CONVERGE_TIMEOUT`) without disturbing the ContextVar region (`app/tenancy/seam.py::bind_from`).
                session.rollback()
                _converge_one_node(slug, node_id, actor)


@DBOS.workflow()
def converge_shared_account(slug: str, node_id: str, origin_node_id: str, actor: str) -> None:
    """One durable converge of one shared account's managed layer on one node.

    Imported for its decorator side effect by `app/durable/runtime.py`; an unregistered workflow cannot be
    enqueued or recovered.

    Only identifiers travel (slug, target, origin, actor label); the step re-reads everything, so a
    recovered run converges the fleet as it is. A single step: the one effect's receipt is the audit row
    the same step writes, and the audit and the raise are one decision."""
    converge_on_node(slug, node_id, origin_node_id, actor)


def enqueue_converge(slug: str, node_id: str, origin_node_id: str, actor: str) -> bool:
    """Hand one node's converge to the durable queue. True when the queue now owns it.

    `True` means "the engine accepted this id" (an idempotent attach); for a second fan-out in the same
    window it reads "already queued". `False` means nothing is queued for that node.

    The window is taken here, per call; the identity is per pair, so straddling a boundary is harmless.

    Never raises: by now the login succeeded and the account is dispatchable, so an unreachable DBOS must
    not turn into a 500. A failure is one warning naming the account and node, and a False."""
    return enqueue_or_warn(
        CONVERGE_QUEUE, converge_workflow_id(slug, node_id, converge_window()), converge_shared_account,
        slug, node_id, origin_node_id, actor, log=log,
        failure=f"account {slug!r}: could not enqueue the converge on node {node_id!r}",
        consequence="that node's own roster-driven trigger and the next runner launch there converge the "
        "same layer",
    )
