"""Run a callback once a session's OUTERMOST transaction commits — the one home for "after commit" work.

`on_commit(session, fn)` owes `fn()` to the transaction (or SAVEPOINT) open at the call. It runs after the
outermost commit — never on a SAVEPOINT release, which fires `after_commit` too — and never at all if the
work rolls back first: a rolled-back SAVEPOINT drops what was owed inside it, a rolled-back transaction drops
everything. A raising callback is logged, never re-raised: the write has committed and must not fail.
`key` collapses repeated calls in one transaction to the first (one wake, however many rows owed it).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from sqlalchemy import event
from sqlalchemy.orm import Session

log = logging.getLogger("app.after_commit")

_PENDING = "after_commit.pending"
_HOOKED = "after_commit.hooked"


def on_commit(session: Session, fn: Callable[[], Any], *, key: Optional[str] = None) -> None:
    """Owe `fn()` once this session's outermost transaction commits (see the module docstring)."""
    pending: list[tuple[Any, Optional[str], Callable[[], Any]]] = session.info.setdefault(_PENDING, [])
    # Tagged with the (sub)transaction it was owed in, so a rolled-back SAVEPOINT takes it with it.
    txn = session.get_nested_transaction() or session.get_transaction()
    # A key is already owed only by THIS transaction or one enclosing it: one owed in a deeper SAVEPOINT can
    # still roll back without this call's work.
    if key is not None and any(item[1] == key and _within(txn, item[0]) for item in pending):
        return
    pending.append((txn, key, fn))
    if not session.info.get(_HOOKED):
        session.info[_HOOKED] = True
        event.listen(session, "after_commit", _flush)
        event.listen(session, "after_soft_rollback", _drop_rolled_back)
        event.listen(session, "after_transaction_end", _drop)


def owed(session: Session) -> bool:
    """Whether this session still owes after-commit work (so committing it now would run that work)."""
    return bool(session.info.get(_PENDING))


def _flush(session: Session) -> None:
    if session.get_nested_transaction() is not None:
        return
    pending, session.info[_PENDING] = session.info.get(_PENDING, []), []
    for _txn, _key, fn in pending:
        try:
            fn()
        except Exception:  # noqa: BLE001 — the write has committed; an after-commit step never fails it
            log.warning("after-commit step %r failed", fn, exc_info=True)


def _within(txn: Any, ancestor: Any) -> bool:
    while txn is not None:
        if txn is ancestor:
            return True
        txn = txn.parent
    return False


def _drop_rolled_back(session: Session, previous_transaction: Any) -> None:
    session.info[_PENDING] = [
        item for item in session.info.get(_PENDING, []) if not _within(item[0], previous_transaction)
    ]


def _drop(session: Session, transaction: Any) -> None:
    # The outermost transaction ended without `_flush` taking the list: it rolled back.
    if transaction.parent is None:
        session.info[_PENDING] = []
