"""The intent mechanism: supersede → persist → send, for actions on one pool slot (`slot_intents`), minted by
`app/fleet_verdicts.py::issue_intents`.

The mechanism: retire predecessors before insert, idempotent insert with org derived from the node, refusal
budget carried across a re-mint, loss-tolerant push. Left to the caller: which rows a new intent replaces
(the caller's predicate). `tests/test_intent_rail_static_parity.py` pins the static rail's rows and frames byte-for-byte.
`app/fleet_verdicts.py` owns the static rail's vocabulary; outcome sets arrive here as arguments.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Iterable, Optional, Sequence

from sqlalchemy import case, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app import node_send_intent

log = logging.getLogger("app.intent_rail")


def settle(session, commit: bool) -> None:
    """COMMIT, or merely FLUSH when the caller owns the transaction.

    The flush still assigns the row `id` (so the frame can name it) but stays in the caller's
    transaction, so the intents vanish with the caller's rollback (`routes/tasks/`'s settled-hold kill)."""
    if commit:
        session.commit()
    else:
        session.flush()


def iso(value: Any) -> Optional[str]:
    """One DB timestamp as the wire spells it. `None` stays `None` (an absent staleness token is
    meaningful)."""
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def still_pending(model, retiring: Sequence[str]):
    """The one predicate for "this row is still in the node's executable set", for either rail's table.

    `retiring` is an argument so each rail's outcome vocabulary stays single-homed with that rail.

    `outcome IS NULL` is spelled out because `NOT IN` over NULL is NULL, which would skip the never-acked
    rows."""
    return or_(model.outcome.is_(None), model.outcome.notin_(tuple(retiring)))


class IssuedIntents:
    """The result of a mint: what was newly INSERTED, and the frames waiting to go down the node socket.

    A class so the ordering is something the caller does: a `commit=False` minter must call `.send()`
    after its own commit, never before (see `app/node_send_intent.py`)."""

    # Allocated per pass on every node; slots also make a typo'd attribute an error.
    __slots__ = ("newly", "node_id", "_frames")

    def __init__(
        self,
        newly: list[dict[str, Any]],
        *,
        node_id: Optional[str],
        frames: list[dict[str, Any]],
    ) -> None:
        # The intents actually INSERTED. Callers count them, log them, and hang follow-up work off them.
        self.newly = newly
        # The node's uuid as text: the `app/node_bus.py` registry key (a name repeats across orgs).
        self.node_id = node_id
        self._frames = frames

    def send(self) -> None:
        """Push every newly-inserted intent to the node. Fire-and-forget; NEVER raises.

        Loss-tolerant: a dropped frame costs latency, never the intent (the node's drain serves the
        pending row), so no outcome may depend on a frame landing. A duplicate push is a no-op, since the
        node acks against the row."""
        for frame in self._frames:
            try:
                node_send_intent.send_intent(self.node_id, frame)
            except Exception as exc:  # noqa: BLE001 — a push may never break the mint it describes
                log.debug(
                    "intent-rail: could not push intent %s to %s (%s) — the node's drain covers it",
                    frame.get("id"), self.node_id, exc,
                )


def supersede(
    session,
    *,
    model,
    doomed: Sequence[Any],
    reason: str,
    superseded_outcome: str,
    describe: Callable[[list[tuple[Any, str, Optional[str]]], int], None],
    commit: bool = True,
) -> tuple[int, dict[str, Any]]:
    """RETIRE every still-pending row matching `doomed` → `(count, carry_forward)`.

    Invariant: clearing a precondition produces at most one launch. Called from a rail's mint,
    before the insert — afterwards it would retire the new row — and from a hold's release, for that hold's
    own order (`slot_state.release` → `fleet_verdicts.retire_released_lease`).

    `doomed` is the whole WHERE, including the pending filter (callers fold in `still_pending(...)`), and
    the whole caller-specific part, as a clause list. `commit` is the mint's: supersession and its insert are one decision
    (see `settle`).

    An UPDATE, not a DELETE: refusal history is retired, not erased. `carry_forward` carries the retry
    budget onto the replacement row, or a park could never fire. `describe` renders the operator log
    line from the retired `(id, verb, outcome)` triples and rowcount, since only the caller can name the
    address."""
    # Read before write so the caller can log what was retired; the UPDATE's WHERE remains the authority.
    from sqlalchemy import select  # local: keeps this module's import surface to what every rail needs

    rows = session.execute(
        select(model.id, model.verb, model.outcome, model.refusals, model.refusal_key).where(*doomed)
    ).all()
    superseded = [(row.id, row.verb, row.outcome) for row in rows]
    if not superseded:
        return 0, {}
    # The furthest-along budget wins, only from a row that actually refused (NULL `refusal_key` = never
    # answered, whose 0 would reset a real tally).
    carried = max(
        (r for r in rows if r.refusal_key is not None), key=lambda r: r.refusals, default=None
    )
    carry_forward = (
        {"refusals": carried.refusals, "refusal_key": carried.refusal_key} if carried else {}
    )
    # The node's last reason is kept and the retirement appended, never overwritten — it is the
    # diagnostic a post-mortem needs.
    changed = session.execute(
        update(model)
        .where(*doomed)
        .values(
            outcome=superseded_outcome,
            reason=case(
                (or_(model.reason.is_(None), model.reason == ""), reason),
                else_=model.reason + f" | superseded: {reason}",
            ),
            next_attempt_at=None,
        )
    ).rowcount
    settle(session, commit)
    describe(superseded, changed or 0)
    return changed or 0, carry_forward


def persist(
    session,
    *,
    model,
    intents: Iterable[dict[str, Any]],
    fixed: dict[str, Any],
    columns: Callable[[dict[str, Any]], dict[str, Any]],
    conflict_constraint: str,
    returning: Sequence[Any],
    on_duplicate: Callable[[dict[str, Any]], None],
    commit: bool,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Insert idempotently → `[(intent, persisted row fields), …]`.

    Returns row fields as well as the intent: the frame must name the row `id`, which exists only after
    this insert. `RETURNING`, not a read-back, so there is no window for a supersession in between.

    `ON CONFLICT DO NOTHING` makes re-deciding safe; returning the inserted subset lets the caller push
    once per new intent. A statement gets one `ON CONFLICT`, so a second unique is caught via a
    SAVEPOINT per intent instead of poisoning the session.

    `fixed` carries `org_id` as a scalar subquery on the node: this `pg_insert` bypasses
    `app/tenancy/write_seam.py` (an ORM `before_flush` listener), and most minters run under `OPERATOR`
    with no bound org. The node is the derivation the tenancy manifest declares."""
    # Read by attribute, not `_mapping`: `tests/test_fleet_verdicts_reap_notify.py` drives this with a
    # fake session whose rows expose only the `RETURNING` attributes.
    names = [getattr(col, "key", None) or col.name for col in returning]
    newly: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for intent in intents:
        try:
            with session.begin_nested():
                row = session.execute(
                    pg_insert(model)
                    .values(**fixed, **columns(intent))
                    .on_conflict_do_nothing(constraint=conflict_constraint)
                    .returning(*returning)
                ).first()
        except IntegrityError:
            # An undeclared uniqueness tuple. Same meaning as a DO NOTHING hit: already recorded, so not
            # newly decided and must not re-push.
            on_duplicate(intent)
            continue
        if row is not None:
            newly.append((intent, {name: getattr(row, name) for name in names}))
    settle(session, commit)
    return newly


def issue(
    session,
    *,
    model,
    node_id: Optional[str],
    intents: list[dict[str, Any]],
    supersede_for: Callable[[dict[str, Any]], Optional[dict[str, Any]]],
    fixed: dict[str, Any],
    columns: Callable[[dict[str, Any]], dict[str, Any]],
    conflict_constraint: str,
    returning: Sequence[Any],
    on_duplicate: Callable[[dict[str, Any]], None],
    frame: Callable[[dict[str, Any], dict[str, Any]], Optional[dict[str, Any]]],
    commit: bool,
) -> IssuedIntents:
    """The one way an intent comes into existence, on either rail: supersede → persist → send.

    One function because each hand-rolled pairing was a bug; a caller must do none of it (a second
    supersede would retire the row just issued).

    Order: retire predecessors, carry the retry budget forward, insert, then push frames.

    `supersede_for` returns kwargs for one `supersede` call, or `None` for a verb that replaces nothing
    (per intent, since rails differ even on that). `frame` may return `None` to keep the row but skip the
    push (the drain serves it later).

    `commit=True` commits then sends. `commit=False` flushes only and sends nothing; the caller commits
    and calls `.send()` itself."""
    for intent in intents:
        kwargs = supersede_for(intent)
        if kwargs is None:
            continue
        _superseded, carry_forward = supersede(session, commit=commit, **kwargs)
        # The retry budget survives the re-mint; a changed reason still resets it on the new row.
        intent.update(carry_forward)

    persisted = persist(
        session, model=model, intents=intents, fixed=fixed, columns=columns,
        conflict_constraint=conflict_constraint, returning=returning,
        on_duplicate=on_duplicate, commit=commit,
    )
    frames = [f for f in (frame(intent, row) for intent, row in persisted) if f is not None]
    issued = IssuedIntents([intent for intent, _row in persisted], node_id=node_id, frames=frames)
    if commit:
        issued.send()
    return issued
