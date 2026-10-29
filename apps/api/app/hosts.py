"""Hosts: the read model over machines and the nodes they carry, and the naming rules for both.

There is no hosts table. A host is one of:

* a MACHINE host — a `nodes` row with a `vnode_hosts` marker. It runs no work; its nodes are co-located
  `virtual_nodes`. Every new Ubuntu machine is made as one (`app/node_orders.py`).
* a LEGACY host — any other machine `nodes` row (no marker, not co-located). It is projected as a host
  carrying one node of the same name, the same row, and cannot take more nodes. macOS and Windows machines
  always land here (`node_platform.CAP_VNODE_HOST`).

NAMING. A name is a label, never an identity: the id is. Names need not be unique — not across orgs, not within
one — so no door refuses a name another node, host or open order carries; only its SHAPE is checked
(`shape_problem`), since `nodes.name` is also the tailnet hostname. The host's display name N lives on
`vnode_hosts.name`; its own row is the machine handle `N-host`. No node may be named with the `-host` ending, so
a node's name never reads as a host's machine (`routes/nodes/register.py::_validated_new_node_name`). A tenant is never
served the handle. `suggest` still pre-fills a name no node or host in the org carries — a convenience, not a
rule.

VISIBILITY. Everything is read through the caller's seam, so an operator host (org zero) never reaches a tenant;
the tenant's own nodes on one are grouped under a synthetic `shared` entry that names no host. A suggestion's name
read is answered here in raw SQL scoped to one org, as a yes or no, never a row."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional, get_args
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import edition_features, node_host_metrics, node_platform
from app.entity_shape import count_by
from app.models.credentials import CloudConnection
from app.models.hosting import AllowedInstanceType
from app.models.node_groups import NodeProject
from app.models.nodes import Node, NodeOrder
from app.models.worktrees import VirtualNode, VnodeEnrolment, VnodeHost
from app.node_bootstrap import BOOTSTRAP_FETCHED_KEY
from app.node_identity import is_uuid_shaped
from app.tenancy.ids import is_tenant_org

if edition_features.enabled("microvm"):
    from app import microvm_mode

if TYPE_CHECKING:
    from app import vnodes

#: Where a host stands (`project`): the `HostOut.state` vocabulary, and the `state` filter's (`narrow`).
HostState = Literal["provisioning", "ready", "failed", "stopped", "deleting"]
STATES: tuple[str, ...] = get_args(HostState)

#: The ending of a machine host's own `nodes.name`; no node name may carry it.
HOST_SUFFIX = "-host"
#: A DNS label is at most 63 characters, and a machine host's handle adds `HOST_SUFFIX` to its name.
MAX_NAME = 63
MAX_HOST_NAME = MAX_NAME - len(HOST_SUFFIX)
#: The furthest a suggestion counts before giving up and leaving the field for the person to fill. Small: each
#: candidate is a lookup.
_MAX_SUGGESTION = 50

# The plain-language sentences every name check answers with.
NAME_EMPTY = "Enter a name."
NAME_SHAPE = (
    "Use only letters, numbers and hyphens, up to 63 characters, and don't start or end with a hyphen."
)
NAME_TOO_LONG_FOR_HOST = f"A host's name can be at most {MAX_HOST_NAME} characters."
NAME_UUID = "A name can't look like an ID. Pick a readable name."
HOST_SUFFIX_REFUSAL = 'A name can\'t end in "-host": that ending is kept for host machines.'
RENAME_FROM_HOST = "This is a host's machine. Rename the host instead."
LEGACY_REASON = "Re-create this machine to add nodes."
#: A host whose co-located nodes fail for want of a rootless container engine (a machine first booted on a release
#: that ships none, `aws/ec2.py::UBUNTU_RELEASE`): retrying or adding more fails the same way every time.
NO_ENGINE_REASON = (
    "This host has no rootless container engine, so it can't run nodes. Delete it and add a new host."
)
#: The failed order's sentence for the same cause (`node_orders.node_failed_sentence`); here, beside the reason it
#: shares a cause with, because `node_orders` imports this module.
NODE_FAILED_NO_ENGINE = (
    "The node couldn't start: its host has no rootless container engine. Delete the host and add a new one."
)
#: The vnodehelper's reason for "no rootless engine" (the node daemon's `ensure_rootless_engine`),
#: which a host reports as `<reason>: <detail>`.
ENGINE_FAILURE = "docker_failed"

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

# Within one org (`:org`): does a node, a host's display name, or a host's machine row carry `:name`? Only
# `suggest` and a replacement's handle ask, to keep a new label distinct; nothing refuses on it. Another org is
# never consulted — no oracle.
_NAME_IN_USE_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM vnode_hosts WHERE org_id = :org AND name = :name) "
    "OR EXISTS (SELECT 1 FROM nodes WHERE org_id = :org AND name = :name)"
)


def is_host_handle(name: str) -> bool:
    """Does `name` carry the machine-handle ending?"""
    return name.strip().lower().endswith(HOST_SUFFIX)


def handle_for(display_name: str) -> str:
    """The machine handle (`nodes.name`) of a host displayed as `display_name`."""
    return f"{display_name}{HOST_SUFFIX}"


def display_name_of(session: Session, node_id: UUID) -> Optional[str]:
    """A named machine host's display name, or None (not a host, or one made before display names)."""
    return session.scalar(select(VnodeHost.name).where(VnodeHost.node_id == node_id))


def name_in_use(session: Session, name: str, *, org_id: UUID) -> bool:
    """Does a node or a host in `org_id` carry `name`? For keeping a suggested label distinct, never a refusal."""
    return bool(session.execute(_NAME_IN_USE_SQL, {"name": name, "org": org_id}).scalar())


def shape_problem(name: str, *, for_host: bool) -> Optional[str]:
    """The sentence saying what is wrong with the SHAPE of `name` as a new node's name, or None. Pure."""
    name = name.strip()
    if not name:
        return NAME_EMPTY
    if is_uuid_shaped(name):
        return NAME_UUID
    if _NAME_RE.fullmatch(name) is None:
        return NAME_SHAPE
    if is_host_handle(name):
        return HOST_SUFFIX_REFUSAL
    if for_host and len(name) > MAX_HOST_NAME:
        return NAME_TOO_LONG_FOR_HOST
    return None


def suggest(session: Session, base: str, *, org_id: UUID, start: int, for_host: bool = False) -> Optional[str]:
    """`{base}-{n}` with n the smallest integer >= `start` no node or host in `org_id` carries (and, `for_host`,
    whose `-host` handle none carries either), or None when none fits. A pre-fill only: the person may keep any
    well-shaped name, a duplicate included.

    The base is cut so the result stays a valid name."""
    limit = MAX_HOST_NAME if for_host else MAX_NAME
    for n in range(start, _MAX_SUGGESTION + 1):
        suffix = f"-{n}"
        candidate = f"{base[: limit - len(suffix)].rstrip('-')}{suffix}"
        if shape_problem(candidate, for_host=for_host) is not None or name_in_use(session, candidate, org_id=org_id):
            continue
        if for_host and name_in_use(session, handle_for(candidate), org_id=org_id):
            continue
        return candidate
    return None


def rename_host(session: Session, host_node_id: UUID, new_name: str) -> str:
    """Rename a machine host: its display name and its machine handle together, in the caller's transaction.
    Returns the trimmed name; raises `ValueError` carrying the sentence that says why not."""
    new_name = new_name.strip()
    marker = session.scalar(select(VnodeHost).where(VnodeHost.node_id == host_node_id))
    row = session.scalar(select(Node).where(Node.id == host_node_id))
    if row is None:
        raise LookupError("unknown host")
    if marker is None:
        raise ValueError("Rename this machine from its node's settings.")
    current = marker.name or row.name
    if new_name == current:
        return new_name
    # The shape only: a name another node or host carries is allowed (names are labels).
    problem = shape_problem(new_name, for_host=True)
    if problem is not None:
        raise ValueError(problem)
    marker.name = new_name
    row.name = handle_for(new_name)
    return new_name


# ── The projection ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reader:
    """Who is reading: their org, whether they are on the operator plane (operator-only fields and rows), and
    whether they administer their org (`Principal.is_org_admin`) — only an admin reads a failed order whose machine
    is gone, as only an admin may retry or dismiss it."""

    org_id: Optional[UUID]
    is_operator: bool
    is_org_admin: bool = False


def _cp_launched(row: Node) -> bool:
    infra = row.infra or {}
    return bool(infra.get("instance_id")) or infra.get("launched_by") == "control-plane"


def machine_is_up(row: Node) -> bool:
    """Has this machine reported in at least once? A launched one has an instance; any one has been seen."""
    return row.last_seen is not None


def _waiting_reason(row: Node) -> str:
    infra = row.infra or {}
    if _cp_launched(row) and not infra.get("instance_id"):
        return "Starting the machine."
    if _cp_launched(row):
        if edition_features.enabled("cloud_nodes"):
            from app import cloud_boot_watch

            # What its boot log says, once it has stayed silent long enough to be read (`app/cloud_boot_watch.py`).
            report = infra.get(cloud_boot_watch.BOOT_REPORT_KEY)
            if isinstance(report, dict) and isinstance(report.get("reason"), str) and report["reason"]:
                return str(report["reason"])
        return "Booting. Waiting for the machine to report in."
    return "Waiting for the machine to join."


def held_in_first_boot(session: Session, node_ids: list[Optional[UUID]]) -> set[UUID]:
    """Of these nodes, those whose machine order still waits out its new host's first boot
    (`node_orders.on_vnodes_failed`): the host retries them, and their failure is not shown while the order's
    stepper shows none. After a Retry too, while the order is back at `joining`."""
    if not edition_features.enabled("cloud_nodes"):
        return set()  # The community edition has no node orders.
    from app.node_orders import MACHINE_PATHS  # Local: `node_orders` imports this module.

    ids = [node_id for node_id in node_ids if node_id is not None]
    if not ids:
        return set()
    return set(session.scalars(
        select(NodeOrder.node_id).where(
            NodeOrder.node_id.in_(ids), NodeOrder.path.in_(MACHINE_PATHS), NodeOrder.stage == "joining",
        )
    ).all())


def shown_state(vnode: VirtualNode, held: set[UUID]) -> str:
    """A vnode's converge state as a person reads it: a failed one `held_in_first_boot` is still provisioning."""
    return "provisioning" if vnode.state == "failed" and vnode.node_id in held else vnode.state


def _node_state(row: Node, vnode: Optional[VirtualNode], held: set[UUID]) -> str:
    if vnode is not None:
        return shown_state(vnode, held)
    return "ready" if machine_is_up(row) else "provisioning"


def _origin(row: Node, reader: Reader) -> str:
    if reader.is_operator and row.org_id is not None and not is_tenant_org(row.org_id):
        return "operator"
    return "cloud" if _cp_launched(row) else "self_hosted"


#: EC2's processor names (`models.INSTANCE_ARCHITECTURES`) in the node's own vocabulary (`node_platform.ARCH_VALUES`).
_EC2_ARCH = {"x86_64": "x64", "arm64": "arm64"}


def _gib(kib: Optional[int], per_gib: int) -> Optional[float]:
    return round(kib / per_gib, 1) if kib else None


def _hardware(
    row: Node, reading: Optional[node_host_metrics.LatestReading], spec: Optional[AllowedInstanceType],
    connection_provider: Optional[str] = None,
) -> dict[str, Any]:
    """What the machine is. Its size is its instance type's EC2 snapshot when there is one — the same figures
    Products › Instance types shows, not the few percent less the kernel reports — else the machine's own latest
    metrics bucket. Its architecture and virtualization are its own `platform` block's word.

    `machine`: `cloud` when the row names a cloud launch, since EC2 says `amazon`/`none` alike for a Nitro VM
    and a metal type; otherwise `systemd-detect-virt`'s word (`none` = physical). Unknown stays null. `cloud`: which
    one (`_cloud_of`; a launch not yet recorded names its connection's, `connection_provider`), null for any other
    machine."""
    infra = row.infra or {}
    instance_type = infra.get("instance_type") or None
    virtualization = row.virtualization
    if instance_type or infra.get("instance_id"):
        machine: Optional[str] = "cloud"
    elif virtualization is None:
        machine = None
    else:
        machine = "physical" if virtualization == "none" else "vm"
    vcpu = spec.vcpu if spec is not None else None
    memory = _gib(spec.memory_mib, 1024) if spec is not None else None
    arch = row.arch or (_EC2_ARCH.get(spec.architecture or "") if spec is not None else None)
    if reading is not None:
        vcpu = vcpu or reading.cpu_count
        memory = memory or _gib(reading.mem_total_kb, 1024 * 1024)
    return {
        "instance_type": instance_type, "vcpu": vcpu, "memory_gib": memory, "arch": arch, "machine": machine,
        "virtualization": virtualization, "kvm": row.kvm, "hypervisor": row.hypervisor,
        # The one sentence the console shows beside "No hypervisor" (`microvm_mode.prereq_sentence`); the
        # community edition has no MicroVMs.
        "hypervisor_blocker": (microvm_mode.prereq_sentence(row)
                               if row.kvm is not None and edition_features.enabled("microvm") else None),
        "cloud": _cloud_of(infra, connection_provider) if machine == "cloud" else None,
    }


def _cloud_of(infra: dict[str, Any], connection_provider: Optional[str] = None) -> Optional[str]:
    """The cloud `infra` names (`clouds.provider_key_of`: none named is AWS); None for a key no adapter knows.

    The key is written only once the launch records its instance (`durable/cloud_node.py::_record`): until then — a
    launch still running, or one that failed before it — the row's connection names the cloud, so a GCP or Azure
    machine never reads as EC2 while it provisions."""
    if not edition_features.enabled("cloud_nodes"):
        return None  # The community edition has no clouds.
    from app import clouds

    if not infra.get(clouds.INFRA_PROVIDER_KEY) and connection_provider:
        infra = {**infra, clouds.INFRA_PROVIDER_KEY: connection_provider}
    try:
        return clouds.provider_key_of(infra)
    except ValueError:
        return None


def _failure_text(vnode: VirtualNode, reader: Reader) -> Optional[str]:
    """A co-located node's failure as `reader` sees it: on a host of another org (the operator's machine carrying a
    tenant's node), its NodeLink mount text may name the operator's mount target, so AWS ids and addresses are cut."""
    if vnode.failure_reason is None or reader.is_operator or vnode.org_id == reader.org_id:
        return vnode.failure_reason
    if not edition_features.enabled("nodelink"):
        return vnode.failure_reason  # The community edition has no NodeLink and one org.
    from app import nodelinks  # Local: `nodelinks` imports the node routes' neighbours.

    return nodelinks.scrub_operator_ids(vnode.failure_reason)


def _converge_out(
    vnode: VirtualNode, marker: Optional[VnodeHost], enrolling: set[UUID], reader: Reader,
) -> Optional[dict[str, Any]]:
    """A `joining` order's node as its host's converge has it (jaahd's `vnode_converge`): not yet taken
    (its enrolment unread), being set up, or retrying after a failed try — that failure in the host's words,
    which `held_in_first_boot` keeps off the node itself — and when the host last polled. None once ready."""
    if vnode.state == "failed":
        step = "retrying"
    elif vnode.state != "provisioning":
        return None
    else:
        step = "pickup" if vnode.id in enrolling else "building"
    return {
        "step": step,
        "error": _failure_text(vnode, reader) if step == "retrying" else None,
        "polled_at": marker.desired_polled_at if marker is not None else None,
    }


#: How many failed orders with no machine left (`_failed_orders`) the roster lists, newest first.
FAILED_ORDERS_LIMIT = 50
#: How long a failed order with no machine left stays listed (`_failed_orders`) unless dismissed: older ones are history.
FAILED_ORDERS_FOR = timedelta(days=7)
#: Where a failed order's machine would have come from, by its path (`node_orders.MACHINE_PATHS`).
_PATH_ORIGIN = {"cloud": "cloud", "self_hosted": "self_hosted", "rent_dedicated": "rented"}


def _order_out(order: NodeOrder, *, instance_started: bool, install_fetched_at: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": str(order.id), "stage": order.stage, "node_id": str(order.node_id) if order.node_id else None,
        "name": order.name, "error": order.error,
        # The stage a failed order failed in, which a retry re-runs; None unless failed.
        "failed_stage": order.failed_stage,
        # How long it has stood in its stage: a stuck order is told apart from a slow one.
        "created_at": order.created_at, "updated_at": order.updated_at,
        # Its machine exists at the cloud: "launching" is then a boot, not a launch still in flight.
        "instance_started": instance_started,
        # When its machine last fetched the install script; None = the install command has not run.
        "install_fetched_at": install_fetched_at,
    }


def _failed_orders(session: Session, reader: Reader, org_id: Optional[UUID]) -> list[dict[str, Any]]:
    """A `failed_order` entry for each failed, undismissed machine-path order whose machine row is gone — a launch's
    compensation or a never-joined expiry deletes it, and `host_node_id` is `SET NULL` — so the failure stays on
    the roster, with its Retry and Dismiss, until the person dismisses it or `FAILED_ORDERS_FOR` passes since it
    last changed. It names no host: there is none. An admin's (or the operator's) read only; a tenant's is its own
    org's, `org_id` narrows the operator's.
    An order the person ended by deleting its machine is dismissed then (`node_orders.end_for_deleted_node`)."""
    if not (reader.is_operator or reader.is_org_admin):
        return []
    if not edition_features.enabled("cloud_nodes"):
        return []  # The community edition has no node orders.
    from app import node_orders  # Local: `node_orders` imports this module.
    from app.models.orgs import in_live_org

    query = select(NodeOrder).where(
        NodeOrder.stage == node_orders.FAILED, NodeOrder.dismissed_at.is_(None),
        NodeOrder.host_node_id.is_(None), NodeOrder.path.in_(node_orders.MACHINE_PATHS),
        NodeOrder.updated_at > datetime.now(timezone.utc) - FAILED_ORDERS_FOR, in_live_org(NodeOrder.org_id),
    )
    if not reader.is_operator:
        query = query.where(NodeOrder.org_id == reader.org_id)
    elif org_id is not None:
        query = query.where(NodeOrder.org_id == org_id)
    out: list[dict[str, Any]] = []
    for order in session.scalars(
        query.order_by(NodeOrder.created_at.desc(), NodeOrder.id).limit(FAILED_ORDERS_LIMIT)
    ).all():
        entry: dict[str, Any] = {
            "id": None, "name": order.name, "kind": "failed_order", "state": "failed", "state_reason": order.error,
            "origin": _PATH_ORIGIN.get(order.path), "os": order.os, "hardware": None, "operator_allocated": False,
            "can_add_nodes": False, "add_nodes_reason": None, "rental": None, "nodes": [],
            "order": _order_out(order, instance_started=False),
        }
        if reader.is_operator:
            entry["org_id"] = str(order.org_id)
        out.append(entry)
    return out


def project(
    session: Session, rows: list[Node], reader: Reader, *, org_id: Optional[UUID] = None,
) -> list[dict[str, Any]]:
    """Every host the reader may see, from the node rows they may see, then each failed order whose machine is gone
    (`_failed_orders`; `org_id` is the operator's org filter). Read through the caller's seam."""
    by_id = {row.id: row for row in rows}
    facts: dict[UUID, vnodes.Hosting] = {}
    if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
        from app import vnodes

        facts = vnodes.hosting_facts(session, by_id)
    host_ids = {node_id for node_id, fact in facts.items() if fact.is_host}
    colocated = {node_id: fact for node_id, fact in facts.items() if not fact.is_host}

    # Each co-located node's placement, where the reader may see it (an operator host's is in org zero).
    placements: dict[UUID, VirtualNode] = {
        vnode.node_id: vnode
        for vnode in session.scalars(
            select(VirtualNode).where(VirtualNode.node_id.in_(list(colocated)))
        ).all()
        if vnode.node_id is not None
    } if colocated else {}
    if reader.is_operator:
        # An operator sees every host: one carrying a node of a filtered org is listed with it.
        missing = {v.host_node_id for v in placements.values()} - set(by_id)
        if missing:
            for row in session.scalars(select(Node).where(Node.id.in_(list(missing)))).all():
                by_id[row.id] = row
                host_ids.add(row.id)
    markers = {
        marker.node_id: marker
        for marker in session.scalars(select(VnodeHost).where(VnodeHost.node_id.in_(list(host_ids)))).all()
    } if host_ids else {}
    carried: dict[UUID, list[VirtualNode]] = {}
    if host_ids:
        for vnode in session.scalars(
            select(VirtualNode).where(VirtualNode.host_node_id.in_(list(host_ids))).order_by(VirtualNode.slot)
        ).all():
            carried.setdefault(vnode.host_node_id, []).append(vnode)
    names = {row.id: row for row in by_id.values()}
    if reader.is_operator:
        # The operator's host rows list every node they carry, an org filter notwithstanding. Never for a
        # tenant reader, whose list stays exactly the nodes their own grants show.
        extra = {v.node_id for vs in carried.values() for v in vs if v.node_id is not None} - set(names)
        if extra:
            for row in session.scalars(select(Node).where(Node.id.in_(list(extra)))).all():
                names[row.id] = row
    counted = set(names) | set(by_id)
    project_counts = count_by(session, NodeProject.node_id, list(counted))
    orders: dict[UUID, NodeOrder] = {}
    if by_id:
        for order in session.scalars(
            select(NodeOrder).where(NodeOrder.host_node_id.in_(list(by_id)))
            .distinct(NodeOrder.host_node_id).order_by(NodeOrder.host_node_id, NodeOrder.created_at.desc())
        ).all():
            orders[order.host_node_id] = order
        # Only the NEWEST order is the host's: a later one that finished supersedes an older failure,
        # and a dismissed (failed) one is off the list — the person has seen it (`POST .../dismiss`) — without
        # resurfacing an older failure beneath it.
        orders = {
            host: order for host, order in orders.items() if order.stage != "ready" and order.dismissed_at is None
        }

    # The hosting behind each hosted host (Dedicated) or machine node (a legacy trial); the community edition has none.
    # Imported here: the billing engine imports the order pipeline, which imports this module.
    hosted: dict[UUID, dict[str, Any]] = {}
    if edition_features.enabled("hostings"):
        from app.billing import hostings as billing_hostings

        hosted = billing_hostings.for_machines(session, list(by_id))

    # Every projected machine's hardware in two batched reads: the newest metrics bucket of each (an index
    # seek apiece, `node_host_metrics.latest_map`) and the EC2 snapshot of each instance type named.
    machine_ids = [node_id for node_id in by_id if node_id not in colocated]
    readings = node_host_metrics.latest_map(session, machine_ids)
    types = {str(by_id[i].infra["instance_type"]) for i in machine_ids if (by_id[i].infra or {}).get("instance_type")}
    specs = {
        spec.instance_type: spec
        for spec in session.scalars(
            select(AllowedInstanceType).where(AllowedInstanceType.instance_type.in_(list(types)))
        ).all()
    } if types else {}

    # The cloud of each launch whose row does not name one yet, from its connection (`_cloud_of`); read through the
    # caller's seam, so one it cannot see stays unnamed (AWS), as before. The community edition has no clouds.
    pending: set[str] = set()
    if edition_features.enabled("cloud_nodes"):
        from app import clouds

        pending = {
            str((by_id[i].infra or {})["connection_id"]) for i in machine_ids
            if (by_id[i].infra or {}).get("connection_id") and not (by_id[i].infra or {}).get(clouds.INFRA_PROVIDER_KEY)
        }
    pending_ids = [UUID(c) for c in pending if is_uuid_shaped(c)]
    connection_clouds = {
        str(conn_id): provider
        for conn_id, provider in session.execute(
            select(CloudConnection.id, CloudConnection.provider).where(CloudConnection.id.in_(pending_ids))
        ).tuples().all()
    } if pending_ids else {}

    def hardware_out(row: Node) -> dict[str, Any]:
        infra = row.infra or {}
        return _hardware(
            row, readings.get(row.id), specs.get(str(infra.get("instance_type"))),
            connection_clouds.get(str(infra.get("connection_id"))),
        )

    held = held_in_first_boot(session, [v.node_id for vs in carried.values() for v in vs])

    def node_out(row: Node, vnode: Optional[VirtualNode], *, host_stopped: bool = False) -> dict[str, Any]:
        stopped = host_stopped or _stopped(hosted.get(row.id))
        return {
            "id": str(row.id), "name": row.name, "state": "stopped" if stopped else _node_state(row, vnode, held),
            "project_count": int(project_counts.get(row.id, 0)),
            # The host's own report, else it lives only in the machine's journal.
            "failure_reason": _failure_text(vnode, reader) if vnode is not None and vnode.node_id not in held else None,
            "updated_at": vnode.updated_at if vnode is not None else None,
        }

    # Each joining order's node on its host, and which of them the host has not yet taken (its envelope unread).
    joining = {
        host_id: vnode
        for host_id, order in orders.items()
        if order.stage == "joining" and order.node_id is not None
        for vnode in carried.get(host_id, [])
        if vnode.node_id == order.node_id
    }
    enrolling = set(session.scalars(
        select(VnodeEnrolment.virtual_node_id).where(
            VnodeEnrolment.virtual_node_id.in_([v.id for v in joining.values()])
        )
    ).all()) if joining else set()

    def order_out(host_id: UUID) -> Optional[dict[str, Any]]:
        order = orders.get(host_id)
        if order is None:
            return None
        infra = by_id[host_id].infra or {}
        fetched = infra.get(BOOTSTRAP_FETCHED_KEY)
        out = _order_out(
            order, instance_started=bool(infra.get("instance_id")),
            install_fetched_at=fetched if isinstance(fetched, str) else None,
        )
        if host_id in joining:
            out["converge"] = _converge_out(joining[host_id], markers.get(host_id), enrolling, reader)
        return out

    out: list[dict[str, Any]] = []
    for host_id in host_ids:
        row = by_id[host_id]
        marker = markers.get(host_id)
        live = [v for v in carried.get(host_id, []) if v.node_id is not None and v.node_id in names]
        state, reason = _machine_state(row, marker, carried.get(host_id, []), hosted.get(host_id))
        nodes = [node_out(names[v.node_id], v, host_stopped=state == "stopped") for v in live]
        failed = orders.get(host_id)
        can_add, add_reason = _may_add(
            row, marker, state, reader, carried.get(host_id, []),
            failed.error if failed is not None and failed.stage == "failed" else None,
        )
        entry = {
            "id": str(row.id),
            "name": (marker.name if marker is not None and marker.name else row.name),
            "kind": "machine",
            "state": state,
            "state_reason": reason,
            "origin": _origin(row, reader),
            "os": row.os,
            "hardware": hardware_out(row),
            # The operator allocated it to its tenant, who may not delete it (`app/node_allocation.py`).
            "operator_allocated": bool(row.operator_allocated),
            "can_add_nodes": can_add,
            "add_nodes_reason": add_reason,
            "rental": hosted.get(host_id),
            "nodes": nodes,
            "order": order_out(host_id),
        }
        if reader.is_operator:
            entry["machine_name"] = row.name
            entry["org_id"] = str(row.org_id) if row.org_id is not None else None
            entry["instance"] = _instance(row)
            entry["shared_capacity"] = marker.shared_capacity if marker is not None else None
            entry["boot_log"] = _boot_log(row)
        out.append(entry)

    shared: list[dict[str, Any]] = []
    for node_id, row in by_id.items():
        if node_id in host_ids:
            continue
        fact = colocated.get(node_id)
        if fact is not None:
            if node_id in placements and placements[node_id].host_node_id in host_ids:
                continue  # listed under its host
            # Co-located on a host this reader may not see: the tenant's own node on an operator host.
            shared.append(node_out(row, None))
            continue
        hosting = hosted.get(node_id)
        if _stopped(hosting):
            state, reason = "stopped", stopped_reason(hosting)
        else:
            state = "ready" if machine_is_up(row) else "provisioning"
            reason = None if state == "ready" else _waiting_reason(row)
        entry = {
            "id": str(row.id),
            "name": row.name,
            "kind": "legacy",
            "state": state,
            "state_reason": reason,
            "origin": _origin(row, reader),
            "os": row.os,
            "hardware": hardware_out(row),
            "operator_allocated": bool(row.operator_allocated),
            "can_add_nodes": False,
            "add_nodes_reason": LEGACY_REASON,
            "rental": hosting,
            "nodes": [node_out(row, None)],
            "order": order_out(node_id),
        }
        if reader.is_operator:
            entry["machine_name"] = row.name
            entry["org_id"] = str(row.org_id) if row.org_id is not None else None
            entry["instance"] = _instance(row)
            entry["boot_log"] = _boot_log(row)
        out.append(entry)
    out.extend(_failed_orders(session, reader, org_id))
    # By name, the id breaking a tie: the roster's default order (`routes/hosts.py::list_hosts`).
    out.sort(key=lambda entry: (str(entry["name"] or "").lower(), str(entry["id"] or "")))
    if shared:
        # Names no host, carries no id: the tenant must not be able to tell which machine it is.
        out.append({
            "id": None, "name": None, "kind": "shared", "state": "ready", "state_reason": None,
            "origin": None, "os": None, "hardware": None, "operator_allocated": False, "can_add_nodes": False,
            "add_nodes_reason": "These nodes run on shared machines.", "rental": None,
            "nodes": sorted(shared, key=lambda n: n["name"].lower()), "order": None,
        })
    return out


def _instance(row: Node) -> Optional[dict[str, Any]]:
    """The cloud instance behind a machine — `{instance_id, region, account_id, image_id, provider, zone, scope}` off
    `infra` (the last three `clouds.InstanceRef`'s, for the console links) — else None. Operator reader only: for a
    hosted machine the account is Jaah's own."""
    infra = row.infra or {}
    if not infra.get("instance_id"):
        return None
    return {
        "instance_id": str(infra["instance_id"]),
        "region": str(infra["region"]) if infra.get("region") else None,
        "account_id": str(infra["account_id"]) if infra.get("account_id") else None,
        "image_id": str(infra["image_id"]) if infra.get("image_id") else None,
        "provider": _cloud_of(infra),
        "zone": str(infra["zone"]) if infra.get("zone") else None,
        "scope": str(infra["scope"]) if infra.get("scope") else None,
    }


def _boot_log(row: Node) -> Optional[dict[str, Any]]:
    """The tail of a silent machine's console as the boot watch last read it (`app/cloud_boot_watch.py`), already
    secret-cut: `{lines, at}`, else None — and None once the machine reported in, as the log is then history.
    Operator reader only: the console names the operator's own machinery."""
    if row.last_seen is not None:
        return None
    if not edition_features.enabled("cloud_nodes"):
        return None  # The community edition has no cloud boot watch.
    from app import cloud_boot_watch

    report = (row.infra or {}).get(cloud_boot_watch.BOOT_REPORT_KEY)
    if not isinstance(report, dict):
        return None
    tail = report.get("tail")
    if not isinstance(tail, list):
        return None
    return {"lines": [str(line) for line in tail], "at": report.get("at")}


#: Why a stopped hosting's machine is stopped, by `stop_cause` — the plain reason a stopped host shows (AC1.4).
STOPPED_REASONS: dict[Optional[str], str] = {
    "non_payment": "Stopped because its payment failed. Pay the open invoice on the Billing page to start it.",
    "cancel": "Stopped because its hosting ended. Resume it on the Billing page to start it.",
    "plan_end": "Stopped because its plan no longer includes it. Keep it from the Billing page to start it.",
}


def _stopped(hosting: Optional[dict[str, Any]]) -> bool:
    return hosting is not None and hosting.get("status") == "stopped"


def stopped_reason(hosting: Optional[dict[str, Any]]) -> str:
    cause = (hosting or {}).get("stop_cause")
    return STOPPED_REASONS.get(cause, "Stopped. See the Billing page.")


def _machine_state(
    row: Node, marker: Optional[VnodeHost], vnodes_on_it: list[VirtualNode],
    hosting: Optional[dict[str, Any]] = None,
) -> tuple[str, Optional[str]]:
    if _stopped(hosting):
        return "stopped", stopped_reason(hosting)
    if marker is not None and marker.failure_reason:
        return "failed", "This host reported a problem. Delete it and add a new one."
    if vnodes_on_it and all(v.state == "deleting" for v in vnodes_on_it):
        return "deleting", "Removing its nodes."
    if not machine_is_up(row):
        return "provisioning", _waiting_reason(row)
    return "ready", None


def lacks_engine(
    vnodes_on_it: list[VirtualNode], failed_order_error: Optional[str] = None, *, now: Optional[datetime] = None,
) -> bool:
    """Has this host shown it cannot run a node at all: a node failed for want of a rootless engine (its own
    report, or the failed order that outlived it), and none is running (a running one proves the engine is there,
    so the failure was something else). A node placed inside `node_orders.FIRST_BOOT_WINDOW` does not count: a new
    host's first boot is still installing the engine, and that failure clears on its own."""
    if not edition_features.enabled("multi_node_hosts"):
        return False  # The community edition has no Multi-Node Hosts.
    from app.node_orders import FIRST_BOOT_WINDOW  # Local: `node_orders` imports this module.

    if any(v.state == "ready" for v in vnodes_on_it):
        return False
    cutoff = (now or datetime.now(timezone.utc)) - FIRST_BOOT_WINDOW
    return failed_order_error == NODE_FAILED_NO_ENGINE or any(
        v.state == "failed" and (v.failure_reason or "").startswith(ENGINE_FAILURE)
        and v.created_at is not None and v.created_at < cutoff
        for v in vnodes_on_it
    )


def _may_add(
    row: Node, marker: Optional[VnodeHost], state: str, reader: Reader,
    vnodes_on_it: Optional[list[VirtualNode]] = None, failed_order_error: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    if not node_platform.supports(row.os or node_platform.OS_UBUNTU, node_platform.CAP_VNODE_HOST):
        return False, LEGACY_REASON
    # What the host machine itself reported (rootless Docker), decided on its own row.
    no_host = node_platform.node_reason(row, node_platform.CAP_VNODE_HOST)
    if no_host is not None:
        return False, no_host
    if lacks_engine(vnodes_on_it or [], failed_order_error):
        return False, NO_ENGINE_REASON
    if state in ("failed", "deleting"):
        return False, "This host can't take new nodes right now."
    if state != "ready":
        return False, "This host isn't ready yet."
    if edition_features.enabled("multi_node_hosts"):
        from app import vnodes

        if marker is not None and vnodes.host_is_full(marker):
            return False, "This host is full."
    if not reader.is_operator and row.org_id != reader.org_id:
        return False, "You can't add nodes to this host."
    return True, None


def narrow(entries: list[dict[str, Any]], q: Optional[str], state: Optional[str]) -> list[dict[str, Any]]:
    """The `entries` (from `project`) that match `q` and `state`; either blank or None is no filter. `q` is a
    trimmed, case-insensitive substring of the entry's `name`, of a node it carries (the question is as often
    "which host runs node X") or, where the reader is an operator, its `machine_name`. The synthetic `shared`
    entry names no machine, so it stays while `q` is blank, is part of the word `shared` — what the person sees
    it listed as — or names one of its nodes. `state` is one of `STATES`; the caller checks."""
    needle = (q or "").strip().lower()

    def named(entry: dict[str, Any]) -> bool:
        carried = [node["name"] for node in entry.get("nodes", [])]
        if entry["kind"] == "shared":
            return needle in "shared" or any(needle in str(name).lower() for name in carried if name)
        return any(
            needle in str(name).lower() for name in (entry["name"], entry.get("machine_name"), *carried) if name
        )

    return [
        e for e in entries
        if (state is None or e["state"] == state) and (not needle or named(e))
    ]

