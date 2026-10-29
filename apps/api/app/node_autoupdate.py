"""Whether a node installs new releases on its own: the single home of the resolution.

Precedence, most specific first:
  · a node on a Multi-Node Host follows its host — the host's package is its package, so its
    own override is stored but never consulted;
  · the node's own `nodes.autoupdate_override` (on a host's row: the host's override);
  · the Settings knob `node_autoupdate_enabled` (operator tier, default on).

Consumers: `GET /api/agent/autoupdate` (the daemon and the root updaters ask it before every converge) and
the node pages (`routes/nodes/node_wire.py::_node_out`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features
from app.operator_settings import reads as settings_reads
from app.models.nodes import Node

if TYPE_CHECKING:
    from app import vnodes

KNOB = "node_autoupdate_enabled"

# Where the effective value came from; the page words each one.
SOURCE_NODE = "node"
SOURCE_HOST = "host"
SOURCE_SETTINGS = "settings"


class Resolved(NamedTuple):
    enabled: bool
    source: str


def settings_default() -> bool:
    """The Settings knob, resolved now (operator tier: no org)."""
    return settings_reads.get_bool(KNOB)


def resolve(override: Optional[bool], hosting: Optional[vnodes.Hosting], default: bool) -> Resolved:
    """One node's effective value from its own override, its hosting fact and the Settings value."""
    if hosting is not None and not hosting.is_host:
        host = hosting.host_autoupdate_override
        return Resolved(default if host is None else host, SOURCE_HOST)
    if override is not None:
        return Resolved(override, SOURCE_NODE)
    return Resolved(default, SOURCE_SETTINGS)


def for_node(session: Session, node_id: Optional[UUID]) -> Resolved:
    """`node_id`'s effective value; Settings alone for no node or an unknown one."""
    default = settings_default()
    if node_id is None:
        return Resolved(default, SOURCE_SETTINGS)
    override = session.execute(select(Node.autoupdate_override).where(Node.id == node_id)).scalar_one_or_none()
    if not edition_features.enabled("multi_node_hosts"):
        return resolve(override, None, default)  # The community edition has no Multi-Node Hosts.
    from app import vnodes

    hosting = vnodes.hosting_facts(session, [node_id]).get(node_id)
    return resolve(override, hosting, default)
