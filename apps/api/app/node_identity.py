"""Node identity for apps/api: `NODE_ID` resolution and the single home of uuid → row resolution
(contract S2). `apps/jaahd/main.py::resolve_node_id` must agree (the two apps never import each other).

The uuid is the only identity (node.env, socket, dispatch/lease keys, console paths); `name` is a display
and tailnet handle, never unique (`app/hosts.py` NAMING), so a name never resolves to a row. Wire
doors (`wire_node_id*`, `is_registered_node`, `ids_by_wire_value`, `bus_identity`) accept a uuid only,
loudly refusing names; operator doors (`node_id_or_*`, `node_matches`, `node_org`, `node_ref`, `bus_key`)
accept a uuid only too, quietly: a non-uuid resolves nothing. Outbound (uuid → name) is `name_expr` /
`names_by_id`.

Matching is exact (no case-folding, prefix or substring): fuzzy identity would let one node pose as another.
Ingest fails open (`*_or_none`: land the row with a NULL reference); dispatch fails closed (`*_or_404`).
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import false, select
from sqlalchemy.orm import Session

from app.entity_shape import optional_uuid
from app.models.nodes import Node
from app.tenancy import seam
from app.tenancy.ids import ORG_ZERO_ID

log = logging.getLogger(__name__)


def wire_node_id(session: Session, value: Optional[str]) -> Optional[UUID]:
    """`nodes.id` for a value a daemon presented — uuid only — or None.

    A name here is a misprovisioned node, not a spelling to resolve. None, never a raise (ingest fails
    open); request-shaped callers use `wire_node_id_or_404`. A uuid naming no row is None too."""
    if not value:
        return None
    parsed = optional_uuid(value)
    if parsed is None:
        warn_name_on_the_wire(value)
        return None
    return session.scalar(select(Node.id).where(Node.id == parsed))


def wire_node_id_or_404(session: Session, value: Optional[str]) -> UUID:
    """`wire_node_id` for a route: an unresolvable value is a 404 whose message says what to do about it.

    Quotes the value sent, so a node.env still carrying a name is diagnosable from the response."""
    resolved = wire_node_id(session, value)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown node '{value}' — `NODE_ID` in /etc/jaah/node.env must be this node's uuid "
                f"(a node NAME is not accepted on the agent plane). Re-enrol it with "
                f"`jaah auth login <uuid> --with-token`."
            ),
        )
    return resolved


def warn_name_on_the_wire(value: str) -> None:
    """One log line for a daemon presenting a non-uuid. Distinct from `warn_unregistered_node`: the row
    may exist but node.env is stale, so the remedy is re-enrol, not register."""
    log.warning(
        "node %r presented a non-uuid NODE_ID — the agent plane is uuid-only, so this "
        "report resolves to nothing. Its /etc/jaah/node.env still holds a name: re-enrol it with "
        "`jaah auth login <uuid> --with-token`.",
        value,
    )


def is_registered_node(node_id: str, session: Session) -> bool:
    """True iff `node_id` has a `nodes` row, i.e. it was registered through the CP.

    `POST /api/nodes` is the sole creator; every self-report path (heartbeat, observation, config
    snapshot, roster sweep) gates on this and only updates, so a token holder cannot mint a fleet member.
    Presence is separate: an unregistered node still shows as present/online in `GET /api/nodes`
    (`app/node_presence.py`). Presence is measured by the node; membership is declared by the CP."""
    return wire_node_id(session, node_id) is not None


# ── contract S2: uuid → row inbound, uuid → name outbound; nothing else is derived ─────────────────


def is_uuid_shaped(value: Optional[str]) -> bool:
    """Is `value` a uuid? The predicate `POST`/`PUT /api/nodes` refuse a name on.

    Every door resolves a uuid, so a node named with another row's uuid text would read as that row's
    identity wherever the name is shown or typed back."""
    return optional_uuid(value) is not None if value else False


def node_id_or_none(session: Session, value: Optional[str]) -> Optional[UUID]:
    """`nodes.id` for `value`, a node uuid, or None.

    A name is None, not a lookup: names repeat across orgs. A uuid naming no row is None too: resolution
    requires an existing row. None/empty gives None, the "unscoped" value of nullable references like
    `accounts.node_id`."""
    parsed = optional_uuid(value) if value else None
    if parsed is None:
        return None
    return session.scalar(select(Node.id).where(Node.id == parsed))


def node_id_or_404(session: Session, value: Optional[str]) -> UUID:
    """`node_id_or_none`, but an unresolvable value is a 404 rather than a NULL reference, quoting the
    caller's own spelling."""
    resolved = node_id_or_none(session, value)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"unknown node '{value}'")
    return resolved


def node_matches(value: Optional[str]):
    """A predicate over `nodes` itself: is this row the node whose uuid is `value`. A non-uuid matches
    no row (fail-closed), never a name.

    For statements already selecting from `nodes`; `node_ref` is the sibling for a child table's
    reference. Takes no session: it is a predicate."""
    parsed = optional_uuid(value) if value else None
    if parsed is None:
        return false()
    return Node.id == parsed


def node_org(session, value: Optional[str]):
    """The org this node belongs to, or `None` when the node is unknown or unstamped.

    `nodes.org_id` is immutable (`app/tenancy/node_org.py`), so this is the org of everything the node
    touches (e.g. `app/port_grid.py::grid`). `None` is never widened to a default org. What a consumer
    does with `None` is its own call (`port_grid.range_starts` vs `port_grid.scalars`); both fail closed.
    One scalar read; callers hoist it to once per pass."""
    from sqlalchemy import select

    return session.scalar(select(Node.org_id).where(node_matches(value)))


def node_ref(value: Optional[str]):
    """Inbound half as an expression: the `nodes.id` for a uuid, usable directly in a WHERE.

    Exists beside `node_id_or_none` because `col == None` renders `IS NULL`: threading a resolved
    `UUID | None` into filters would make a forgotten guard match NULL ("unscoped") rows, a fail-open.
    Here an unresolvable value yields a NULL subquery and `col = NULL` matches nothing: fail-closed.

    Not for writes: a writer resolves explicitly with `node_id_or_none` / `node_id_or_404`. Takes no
    session: it is an expression."""
    parsed = optional_uuid(value) if value else None
    if parsed is None:
        return select(Node.id).where(false()).scalar_subquery()
    return select(Node.id).where(Node.id == parsed).scalar_subquery()


def name_expr(column):
    """Outbound half: a node-uuid `column` rendered as the node's name, resolved at query time.

    A correlated subquery, never a stored copy, so a rename is a rename everywhere. Subquery rather
    than a join to keep callers' row multiplicity; a statement already joining `nodes` should select
    `Node.name`. NULL-preserving."""
    return select(Node.name).where(Node.id == column).scalar_subquery()


def jaah_home_expr(column):
    """A node-uuid `column` rendered as that node's declared Jaah home, resolved at query time.

    `name_expr`'s twin for the payload builders deriving a slot's credential dir
    (`node_clone.resolve_auth_dir`). NULL-preserving; `resolve_auth_dir` fails closed to `""` on NULL."""
    return select(Node.jaah_home).where(Node.id == column).scalar_subquery()


def names_by_id(session: Session, ids: Iterable[Optional[UUID]]) -> dict[UUID, str]:
    """Resolve many node ids to their names in one query — the list-route shape.

    Like `app/actors.py::actor_names`: NULLs dropped, unresolved ids omitted, no N+1."""
    wanted = {node_id for node_id in ids if node_id is not None}
    if not wanted:
        return {}
    rows = session.execute(select(Node.id, Node.name).where(Node.id.in_(wanted))).all()
    return {node_id: name for node_id, name in rows}


def ids_by_wire_value(session: Session, values: Iterable[Optional[str]]) -> dict[str, UUID]:
    """Batched `wire_node_id`, keyed by the caller's own spelling.

    Uuid only, matching `wire_node_id` exactly: two resolvers disagreeing on one wire value is the defect
    class this module prevents. A non-uuid value is absent from the answer ("not registered")."""
    wanted = {value for value in values if value}
    if not wanted:
        return {}
    parsed = {value: optional_uuid(value) for value in wanted}
    for value, node_id in parsed.items():
        if node_id is None:
            warn_name_on_the_wire(value)
    uuid_keys = {value: node_id for value, node_id in parsed.items() if node_id is not None}
    if not uuid_keys:
        return {}
    live = set(session.scalars(select(Node.id).where(Node.id.in_(set(uuid_keys.values())))).all())
    return {value: node_id for value, node_id in uuid_keys.items() if node_id in live}


def bus_key(session: Session, value: Optional[str]) -> str:
    """The machine key for a node: `str(nodes.id)` when `value` resolves to a row, else `value` itself.

    The one string both sides of a live-socket lookup must agree on: the node-bus and presence registries
    are string-keyed dicts, and the registering side and the dispatching side must be resolved
    through here or a healthy node 503s with `NodeNotConnected`.

    Falls back to the raw value, never None or a raise: presence survives unregistered-ness. A uuid naming
    no row at all keeps that fallback (a deleted node's teardown still addresses its socket by uuid).
    `routes/node_bus.py` resolves once per connection, at `hello`.

    Fails closed itself for a uuid whose row exists outside the caller's scope: the resolution is a scoped
    read, and that uuid's raw value IS the other org's socket key, so it answers `""`, which no socket
    registers under (`routes/node_bus.py::_handshake` refuses an empty `node_id`). The control plane's own
    node is the one exception (`_is_cp_node`): every org's session dispatches to it by `NODE_ID`.

    Quiet on a non-uuid (keyed by the raw value, reaching no socket); `bus_identity` is the
    registration-side twin that logs one."""
    node_id = node_id_or_none(session, value)
    if node_id is not None:
        return str(node_id)
    parsed = optional_uuid(value) if value else None
    if parsed is not None and _exists_outside_scope(session, parsed):
        if _is_cp_node(parsed):
            return str(parsed)
        log.warning("bus_key: node %s is outside the caller's org — withholding its socket", parsed)
        return ""
    return value or ""


def _is_cp_node(node_id: UUID) -> bool:
    """Is `node_id` the control plane's own node, by a uuid `NODE_ID`. A named `NODE_ID` never matches:
    `cp_node_id` resolves it inside the operator org, and that read is the caller's to make."""
    raw = resolve_node_id()
    return raw is not None and is_uuid_shaped(raw) and optional_uuid(raw) == node_id


def _exists_outside_scope(session: Session, node_id: UUID) -> bool:
    """Does `node_id` name a `nodes` row in ANY org — asked after the scoped read missed.

    An identity read (`seam.identity_scope`): it answers only whether a key the caller already holds is a
    machine, and the answer is never shown — it only withholds the socket."""
    with seam.identity_scope():
        return session.scalar(select(Node.id).where(Node.id == node_id)) is not None


def bus_identity(session: Session, value: Optional[str]) -> tuple[str, str]:
    """`(machine key, name)` for one wire value — `bus_key` plus the human half, from one resolution, so
    the two cannot disagree.

    Uuid only, like `bus_key`, but loud: a name here is a misprovisioned daemon, whose socket is then
    keyed by the raw value and unreachable by uuid dispatch (fail-closed). An unresolvable value answers
    `(value, value)`."""
    node_id = wire_node_id(session, value)
    if node_id is None:
        return (value or ""), (value or "")
    return str(node_id), names_by_id(session, [node_id]).get(node_id, value or "")


def warn_unregistered_node(node_id: str, surface: str) -> None:
    """One log line for a self-report from an unregistered id; `surface` names the dropped report."""
    log.warning(
        "node %r reported via %s but is NOT registered — dropping the report rather than creating a row. "
        "If it is real, register it: POST /api/nodes {\"node_id\": %r, \"org_id\": \"<org uuid>\"} — the "
        "org is required and cannot be changed afterwards. Otherwise its NODE_ID is misconfigured and it "
        "is reporting under a name nobody declared.",
        node_id, surface, node_id,
    )


def resolve_node_id() -> Optional[str]:
    """NODE_ID must match the node daemon's own resolution. apps/api is containerized, so its hostname is
    a container id; only the explicit override is trustworthy."""
    return os.environ.get("NODE_ID") or None


def cp_node_id(session: Session) -> Optional[UUID]:
    """The control plane's own `nodes.id`, from its `NODE_ID`, or None (unset or unregistered).

    A uuid resolves by id (`node_id_or_none`). A deployed CP's `NODE_ID` is a NAME ("main1"/"aws1",
    set by the deployment's Terraform), which the uuid-only doors no longer resolve; it is looked
    up once here, inside the operator org only — the CP is the operator's machinery, and a tenant's node of the
    same name is never it. Names are not unique, so two operator nodes carrying it resolve to None (logged),
    never to a guess: set `NODE_ID` to the uuid to settle it. The one home for the CP's own
    row: `routes/commands.py::default_dispatch_node`, `routes/console.py::_resolve_console_node`,
    `routes/health.py::_jaahd_health`, `routes/credentials/` and the compliance resync read it."""
    raw = resolve_node_id()
    if not raw:
        return None
    if is_uuid_shaped(raw):
        return node_id_or_none(session, raw)
    found = session.scalars(select(Node.id).where(Node.org_id == ORG_ZERO_ID, Node.name == raw).limit(2)).all()
    if len(found) > 1:
        log.warning("NODE_ID %r names more than one operator node; set it to the node's uuid", raw)
        return None
    return found[0] if found else None
