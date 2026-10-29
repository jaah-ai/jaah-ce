"""Shared or local, and Syncing… — the Brains page's token facts per account, over `account_fold.account_node_matrix`.

Two reads, layered on the matrix's node entries for `GET /api/resources` (`routes/portfolio.py`):

- `shared_via` per account: the NodeLinks at `nodelinks.ACCOUNTS_MOUNT_PATH` attached to a node whose entry reads
  `token_source: "shared"` — the node's own word (`account_observations.extra`, jaahd
  `shared_storage.token_source`). No chip when every node is local or silent.
- `sync`/`sync_reason` per node entry. After a login, a node attached to the same token-folder NodeLink with no
  observation of the account yet reads `syncing` for `SYNC_PROBE_INTERVALS` account-probe intervals from T0, then
  `not_synced` with its NodeLink's mount reason, or `NO_TOKEN_REASON`. A NodeLink the node reports unmounted is
  `not_synced` at once. Never an open-ended spinner.

T0, per NodeLink, is the later of the account's latest completed login on a node of that NodeLink
(`durable/accounts.py::completed_logins`) and this node's attachment to it (`node_nodelinks.created_at`); with no
login on record, the earliest `shared` observation of the account stands in for the login — the attachment still
counts, so a node attached later gets its own window. Read in a constant number of queries per call, whatever the
account × node count.

A node the matrix does not list (not reporting) is not added: it would read Syncing while nothing can sync it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from app import account_fold, edition_features
from app.operator_settings import reads as settings_reads
from app.clock import as_utc, utcnow
from app.models.credentials import AccountObservation
from app.models.nodelinks import NodeLink, NodeNodeLink
from app.models.nodes import Node

# The community edition has no NodeLink: `annotate` returns before this name is read.
if edition_features.enabled("nodelink"):
    from app import nodelinks

#: How long a node may read `syncing`, in account-probe intervals (`node_account_probe_interval_secs`): the first
#: pass after a login can just miss it, the second cannot (PRD decision D5).
SYNC_PROBE_INTERVALS = 2
#: `sync_reason` when the NodeLink is mounted on the node but no token showed up.
NO_TOKEN_REASON = "No token reported."
SYNCING = "syncing"
NOT_SYNCED = "not_synced"


def _probe_window() -> timedelta:
    return timedelta(seconds=SYNC_PROBE_INTERVALS * settings_reads.get_int("node_account_probe_interval_secs"))


def annotate(
    session, matrix: dict[str, list[dict[str, Any]]], *, now: Optional[datetime] = None,
) -> dict[str, list[dict[str, str]]]:
    """Set `sync` and `sync_reason` on every entry of `matrix` (in place) and return `{slug: shared_via}`, where
    `shared_via` is `[{id, name}]` by name. `matrix` is already narrowed to the caller's nodes."""
    for entries in matrix.values():
        for entry in entries:
            entry["sync"] = None
            entry["sync_reason"] = None
    shared_via: dict[str, list[dict[str, str]]] = {slug: [] for slug in matrix}
    if not edition_features.enabled("nodelink"):
        return shared_via
    rows = session.execute(
        select(NodeLink, NodeNodeLink.node_id, NodeNodeLink.created_at)
        .join(NodeNodeLink, NodeNodeLink.nodelink_id == NodeLink.id)
        .where(NodeLink.mount_path == nodelinks.ACCOUNTS_MOUNT_PATH)
        .order_by(NodeLink.name, NodeLink.id)
    ).all()
    if not rows:
        return shared_via

    links: dict[str, NodeLink] = {}
    members: dict[str, set[str]] = {}
    attached: dict[str, dict[str, datetime]] = {}  # node -> {link: attached at}
    for link, node_id, created_at in rows:
        links[str(link.id)] = link
        members.setdefault(str(link.id), set()).add(str(node_id))
        attached.setdefault(str(node_id), {})[str(link.id)] = as_utc(created_at)

    shared = {
        slug: {
            e["node_id"] for e in entries
            if e.get("present") and e.get("token_source") == account_fold.TOKEN_SOURCE_SHARED
        }
        for slug, entries in matrix.items()
    }
    for slug, shared_nodes in shared.items():
        shared_via[slug] = [
            {"id": link_id, "name": links[link_id].name} for link_id in links if members[link_id] & shared_nodes
        ]
    # Nothing can be syncing unless an attached node is missing an account: then neither T0 read is spent.
    if not any(not e.get("present") and e["node_id"] in attached for entries in matrix.values() for e in entries):
        return shared_via

    from app.durable.accounts import completed_logins  # Deferred: importing it registers DBOS workflows.

    # Only logins on this org's token-folder nodes: the read is scoped to them.
    logins = completed_logins(attached)
    # Through the ORM, so the tenancy seam scopes it to the caller's org like the NodeLink read above.
    since = {
        slug: as_utc(first) for slug, first in session.execute(
            select(AccountObservation.slug, func.min(AccountObservation.ingested_at))
            .where(AccountObservation.extra["token_source"].astext == account_fold.TOKEN_SOURCE_SHARED,
                   AccountObservation.slug.in_(list(matrix)))
            .group_by(AccountObservation.slug)
        ).all()
    }

    pending: list[tuple[dict[str, Any], list[tuple[str, datetime]]]] = []  # (entry, [(link, T0)])
    for slug, entries in matrix.items():
        login_at: dict[str, float] = {}  # node -> its latest login of this account
        for node_id, done in logins.get(slug, []):
            login_at[node_id] = max(done, login_at.get(node_id, done))
        sources = shared[slug] | set(login_at)
        for entry in entries:
            node_id = entry["node_id"]
            if entry.get("present") or node_id not in attached:
                continue
            windows = []
            for link_id in sorted(attached[node_id], key=lambda i: links[i].name):
                if not members[link_id] & sources:
                    continue
                link_logins = [login_at[n] for n in members[link_id] if n in login_at]
                base = datetime.fromtimestamp(max(link_logins), tz=timezone.utc) if link_logins else since.get(slug)
                if base is not None:
                    windows.append((link_id, max(base, attached[node_id][link_id])))
            if windows:
                pending.append((entry, windows))
    if not pending:
        return shared_via

    node_ids = {entry["node_id"] for entry, _ in pending}
    nodes = {str(n.id): n for n in session.scalars(select(Node).where(Node.id.in_(node_ids)))}
    faults = nodelinks.helper_faults({n.id for n in nodes.values()})
    homes = nodelinks.co_located_homes(n.id for n in nodes.values())
    stores = nodelinks.stores_by_id(session, (link.store_id for link in links.values()))
    mounts: dict[tuple[str, str], tuple[str, Optional[str]]] = {}
    current = now or utcnow()
    window = _probe_window()
    for entry, windows in pending:
        node = nodes.get(entry["node_id"])
        if node is None:
            continue
        states = [(_mount(mounts, node, links[link_id], faults, homes, stores), t0) for link_id, t0 in windows]
        # A mounted NodeLink answers for the node; else the first one's reason.
        (mount, reason), t0 = next((s for s in states if s[0][0] == "mounted"), states[0])
        if mount in ("not_mounted", nodelinks.NEEDS_PERMISSION):
            entry["sync"], entry["sync_reason"] = NOT_SYNCED, reason
        elif current < t0 + window:
            entry["sync"] = SYNCING
        else:
            entry["sync"], entry["sync_reason"] = NOT_SYNCED, NO_TOKEN_REASON if mount == "mounted" else reason
    return shared_via


def _mount(
    cache: dict[tuple[str, str], tuple[str, Optional[str]]], node: Node, link: NodeLink, faults: dict,
    homes: dict, stores: dict,
) -> tuple[str, Optional[str]]:
    """`nodelinks.mount_state` for this (node, NodeLink), once per call however many accounts ask."""
    key = (str(node.id), str(link.id))
    if key not in cache:
        cache[key] = nodelinks.mount_state(
            node, link, True, nodelinks.resolve(link.mount_path, node, homes), nodelinks.node_states(node),
            faults.get(node.id), stores.get(link.store_id) if link.store_id else None,
        )
    return cache[key]
