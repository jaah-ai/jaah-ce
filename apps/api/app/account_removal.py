"""The account removals a node still owes, kept until the node carries them out.

An account Delete (`routes/accounts.py::remove_account`) asks every node holding the account to remove it
(`account_delete` for its credential dir, `account_shared_delete` for its token-share leaf). The CP's decision
stands whatever the node's state: a node that is offline, or answers with a failure, gets the removal recorded on
its row (`nodes.owed_account_removals`) and sent again each time it connects (`routes/node_bus.py`), until it
answers `done`. The node kills the account's sessions and force-removes the files (`apps/jaahd/accounts.py`).

An owed removal names its target by node-side path or slug only: the account row is already gone when it is sent.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.nodes import Node

log = logging.getLogger("app.account_removal")

#: The two verbs an account Delete sends, each with the one argument it owes the node.
VERBS = {"account_delete": "config_dir", "account_shared_delete": "slug"}

# One owed removal's round: the node kills the account's sessions, then retries its removal for a few seconds.
_TIMEOUT_SECONDS = 120.0


def owe(session: Session, node_id: UUID, verb: str, args: dict[str, str], actor: str) -> None:
    """Record that NODE_ID still owes VERB with ARGS, once (an equal entry already owed is not added again).
    In the caller's transaction; the caller commits."""
    if verb not in VERBS or not args.get(VERBS[verb]):
        raise ValueError(f"not an account removal: {verb} {args}")
    entry = {"verb": verb, "args": {VERBS[verb]: args[VERBS[verb]]}, "actor": actor}
    owed = session.scalar(select(Node.owed_account_removals).where(Node.id == node_id).with_for_update())
    if owed is None or any(_same(entry, held) for held in owed):
        return
    session.execute(update(Node).where(Node.id == node_id).values(owed_account_removals=[*owed, entry]))


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a.get("verb") == b.get("verb") and a.get("args") == b.get("args")


def settle(wire_node_id: str) -> None:
    """A node connected: send it every removal it owes and forget each one it answers `done`. Never raises (the
    node's dial must not fail on this); what still fails stays owed for its next connect."""
    from app.db import SessionLocal
    from app.entity_shape import optional_uuid
    from app.routes.commands import dispatch_and_audit_blocking
    from app.tenancy import exclusion_log, seam

    node_uuid = optional_uuid(wire_node_id)
    if node_uuid is None:
        return
    try:
        # Detached, then bound to the node's own org: `wire_node_id` is the machine key of the socket the node's
        # own credential opened (`routes/node_bus.py::_handshake`), so the org bound is the caller's own.
        with exclusion_log.caller("account_removal.settle"), seam.detached():
            with SessionLocal() as session, seam.bind_from(session, Node, node_uuid):
                owed = session.scalar(select(Node.owed_account_removals).where(Node.id == node_uuid)) or []
                if not owed:
                    return
                done: list[dict[str, Any]] = []
                for entry in owed:
                    if _carry_out(session, dispatch_and_audit_blocking, wire_node_id, entry):
                        done.append(entry)
                if done:
                    _forget(session, node_uuid, done)
                    session.commit()
    except seam.TenancyRowMissing:
        return  # the node row is gone, and its debts with it
    except Exception as exc:  # noqa: BLE001 — a node's dial must never fail on this bookkeeping
        log.warning("account removals owed by node %s: could not settle (%s: %s)", wire_node_id,
                    type(exc).__name__, exc)


def _carry_out(session: Session, dispatch, node_id: str, entry: dict[str, Any]) -> bool:
    """Send one owed removal; True once the node answered `done`."""
    verb, args = str(entry.get("verb") or ""), entry.get("args")
    if verb not in VERBS or not isinstance(args, dict):
        return True  # malformed: nothing could ever carry it out, so it is not kept
    if _slug_held_again(session, str(args.get(VERBS[verb]) or "")):
        # The slug names a newer account now: carried out, the old debt would delete that one. It lapses.
        log.info("node %s: owed %s %s lapsed — an account holds that slug again", node_id, verb, args)
        return True
    # The pooled connection goes back before the node's round (`seam.bind_from`).
    session.rollback()
    try:
        reply = dispatch(session, node_id, verb, dict(args), str(entry.get("actor") or "system"),
                         timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — offline again, a timeout, a refusal: still owed
        log.info("node %s: owed %s %s not carried out yet (%s: %s)", node_id, verb, args, type(exc).__name__, exc)
        session.rollback()
        return False
    session.commit()  # the audit row `dispatch` wrote
    if str(reply.get("status")) == "done":
        return True
    result: Optional[dict] = reply.get("result") if isinstance(reply.get("result"), dict) else None
    log.warning("node %s: owed %s %s failed again (%s)", node_id, verb, args,
                (result or {}).get("stderr") or reply.get("status"))
    return False


def _slug_held_again(session: Session, target: str) -> bool:
    """Whether an account row holds the slug TARGET names (a slug, or a credential dir ending in one)."""
    from app.models.credentials import Account

    slug = target.rstrip("/").rsplit("/", 1)[-1]
    return bool(slug) and session.scalar(select(Account.id).where(Account.slug == slug).limit(1)) is not None


def _forget(session: Session, node_id: UUID, done: list[dict[str, Any]]) -> None:
    """Drop the DONE entries from the node's owed list, re-read under its row lock so an entry owed meanwhile stays."""
    owed = session.scalar(select(Node.owed_account_removals).where(Node.id == node_id).with_for_update()) or []
    left = [held for held in owed if not any(_same(held, gone) for gone in done)]
    session.execute(update(Node).where(Node.id == node_id).values(owed_account_removals=left))
