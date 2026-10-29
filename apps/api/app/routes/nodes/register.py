"""Node registration: `POST /api/nodes`, the row door `_registered_node`, token re-issue, tailnet re-enrolment."""
from __future__ import annotations

import logging
from typing import Any, Iterable, Literal, Optional
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.orm import Session

from app import edition, edition_features, hosts, jaah_discovery, ce_caps, node_credentials, node_platform, org_limits, node_allocation
from app.actors import stamp_actor
from app.auth import require_user
# Module scope: `assignments` reaches `routes/projects.py` and `routes/pool_slots.py`, neither of which imports this.
from app.routes.assignments import auto_assign_as
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin, session_only
from app.authz.principal import Principal, current_principal
from app.config import get_settings
from app.db import get_session
from app.entity_shape import StrictModel, iso_or_none
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.nodes import Node
# The DNS-label rule, imported: `node_bootstrap` renders the name into a systemd unit and tailnet
# hostname. See `_validated_new_node_name` for why it is needed earlier here.
from app.node_bootstrap import (
    _NODE_NAME_RE,
    BootstrapInputError,
    provisioned_jaah_home,
    FETCH_SHELLS,
    is_undialable,
    render_fetch_command,
    require_bare_origin,
    validate_login_server,
)
from app.node_identity import is_uuid_shaped
from app.access_admin import refuse_foreign_org
from app.tenancy.create_surface import named_org
from app.org_gate import require_org_active
from app.tenancy.ids import is_operator_plane
from app.routes.nodes import reads
from app.routes.nodes.node_wire import (
    NodeCounts,
    NodeDetailOut,
    _cloud_node_errors,
    _narrow_for_tenant,
    _node_out,
    _request_origin,
)
from app.routes.nodes.reads import _counts_map, _node_row
from app.routes.nodes.update import (
    NodeUpdateIn,
    _MICROVM_FIELDS,
    _SUPERADMIN_ONLY_INFRA_KEYS,
    _UUID_SHAPED_NAME_REFUSAL,
    _reject_an_explicit_null_on_a_not_null_column,
)

if edition_features.enabled("cloud_nodes"):
    from app import cloud_node_launch
    from app.cloud_node_launch import CloudNodeError
# The community edition joins no tailnet: `_tailnet_login_server` returns before this name is read.
if edition_features.enabled("tailnet"):
    from app import headscale_keys

router = APIRouter(tags=["nodes"])
log = logging.getLogger("cockpit-nodes")


class NodeTokenIssuedOut(StrictModel):
    """A freshly minted node credential: the only time the plaintext is ever served.

    `app/node_credentials.py` stores only `sha256(token)`; no route returns it again, and a lost token is
    re-issued (also the revocation path).

    `onboarding_command` is server-generated (`node_bootstrap.render_fetch_command` owns the path, header,
    leading space and absence of `-L`), so the browser cannot drift from the channel. It carries the
    token, so it is a secret too, redacted from audit by name (`app/user_activity.py::FULL_TERMS`)."""

    node_id: str
    name: str
    token: str
    issued_at: str
    onboarding_command: str
    # Which shell `onboarding_command` is pasted into, per the node's OS (`node_bootstrap.FETCH_SHELLS`).
    onboarding_shell: Literal["bash", "powershell"] = "bash"
    # The headscale server a self-hosted box's `tailscale up --login-server` dials before it can fetch that
    # command (`_tailnet_login_server`); None = no tailnet configured, and the console says so.
    tailnet_login_server: Optional[str]
    # Whether the console wraps `onboarding_command` in the tailnet join (`_joins_tailnet_first`). Otherwise the
    # line runs alone and the bootstrap it fetches joins the tailnet itself.
    onboarding_joins_tailnet: bool = False


class NodeTailnetReenrolOut(NodeTokenIssuedOut):
    """A re-issued credential whose bootstrap may mint ONE tailnet key until `tailnet_reenrol_until`."""

    tailnet_reenrol_until: str


class NodeRegisteredOut(NodeDetailOut):
    """The 201 from `POST /api/nodes`: the node's record, plus the credential minted alongside it.

    A separate model from `NodeDetailOut`, so no other node response has a token field to remember to
    leave empty; required here, encoding "no node row exists without a credential"."""

    credential: NodeTokenIssuedOut


class NodeCreateIn(NodeUpdateIn):
    """POST body — `node_id` plus the same editable fields: the register-only door into the fleet
    (`CloudNodeCreateIn`/`SelfHostedNodeCreateIn` provision). Self-report paths are update-only.

    `node_id` is the node's `name`, spelled as in `/etc/jaah/node.env`; `name` is not accepted as a
    second spelling.

    `enabled` defaults to true, matching the column (owner's call). Accepted cost: four
    `app/node_liveness.py` alarms gate on `enabled`, so a node enabled before it boots can raise a
    truthful early alarm for a minute or two. Pass `enabled: false` for a node registered ahead of the
    machine existing."""

    node_id: str
    enabled: Optional[bool] = True
    # Declared here, not on `NodeUpdateIn`: a node cannot change org (`trg_nodes_org_id_immutable`), so a
    # PUT field would be a promise the DB answers with a 500. Required, not defaulted (see `_registered_node`).
    org_id: UUID = Field(
        description=(
            "The org this node belongs to for its whole life. Required, and it cannot be changed "
            "afterwards — a node in the wrong org is repaired by deleting it and re-registering."
        )
    )


def _issue_credential(
    session: Session, row: Node, cp_base_url: str, fetch_base_url: Optional[str] = None
) -> NodeTokenIssuedOut:
    """Mint a credential for `row` and render the one response that ever carries its plaintext.

    The mint is `app/node_credentials.py::issue`'s. It does not commit: on registration the credential
    must land in the same transaction as the `nodes` INSERT ("no node without a credential").

    `onboarding_command` names no node: it fetches `node_bootstrap.BOOTSTRAP_PATH`, which resolves the
    node from the presented token (`routes/agent_bootstrap.py`), so identity and the provisioning line
    cannot disagree. `fetch_base_url` is where that line FETCHES from (`console_fetch_origin`), when it is
    not `cp_base_url` and the line does not join the tailnet first; the script it fetches still dials
    `cp_base_url`."""
    plaintext = node_credentials.issue(session, row)
    joins = _joins_tailnet_first(row.os)
    return NodeTokenIssuedOut(
        node_id=str(row.id),
        name=row.name,
        token=plaintext,
        issued_at=iso_or_none(row.node_token_issued_at) or "",
        # Single-homed in `node_bootstrap`, beside the path it dials. A line that joins first fetches over the
        # tailnet it just joined, so from the address nodes dial.
        onboarding_command=_fetch_command(plaintext, cp_base_url if joins else fetch_base_url or cp_base_url, row.os),
        onboarding_shell=FETCH_SHELLS[row.os],
        tailnet_login_server=_tailnet_login_server(session),
        onboarding_joins_tailnet=joins,
    )


def _joins_tailnet_first(os: str) -> bool:
    """Whether a self-hosted one-liner must join the tailnet before it can fetch its bootstrap.

    Only where the console is not public (`Settings.console_public`): a public console serves the bootstrap to
    any machine, and the bootstrap joins the tailnet itself. Only on Ubuntu: the macOS and Windows installers
    install Tailscale and join themselves, so their line keeps fetching from the console."""
    return os == "ubuntu" and not get_settings().console_public


def _tailnet_login_server(session: Session) -> Optional[str]:
    """The login server of the headscale credential a self-hosted bootstrap mints with, or None.

    The same row: `tailscale_credential` with no connection, as `durable/bootstrap_key.py`'s mint reads it, so
    the console's join line and the fetched script name one server. Display only: an unusable setting yields
    None rather than failing the registration — the bootstrap fetch refuses that setting itself, loudly."""
    if not edition_features.enabled("tailnet"):
        return None  # The community edition's node joins no tailnet.
    try:
        credential = cloud_node_launch.tailscale_credential(session)
        if credential is None:
            return None
        server = headscale_keys.login_server(credential)
        validate_login_server(server)
    except (CloudNodeError, KeyError, ValueError) as exc:  # ValueError covers BootstrapInputError
        log.warning("nodes: no usable tailnet login server for the onboarding dialog: %s", exc)
        return None
    return server


def console_fetch_origin(request: Request) -> Optional[str]:
    """The console origin the person is using, for the one-liner to FETCH its bootstrap from — or None.

    The control-plane URL a node dials is often an INTERNAL address, reachable only over the
    tailnet the bootstrap itself joins, so a bare box — or one on another environment's tailnet — cannot fetch
    from it and hangs. `/api/node-bootstrap` is served on the public console too, and
    the browser's own `Origin` header names it with its real scheme (the request URL says `http` behind the
    TLS-terminating ALB). Only an origin naming THIS request's host is taken, and only one `render_fetch_command`
    accepts and a machine can dial; anything else is None, and the caller falls back to the control-plane
    URL."""
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    parts = urlsplit(origin)
    if not origin or parts.hostname != request.url.hostname or parts.port not in (None, request.url.port):
        return None
    try:
        require_bare_origin(origin)
    except BootstrapInputError:
        return None
    return None if is_undialable(origin) else origin


def _fetch_command(plaintext: str, cp_base_url: str, os: str) -> str:
    """`render_fetch_command`, with its refusal translated into this file's operator-facing error.

    The refusal is reachable: the URL piped into the node's shell comes from the `cloud_node_cp_base_url`
    knob or the request origin, and an `http://` value should be a 422 naming the setting, not a 500."""
    try:
        return render_fetch_command(node_token=plaintext, cp_base_url=cp_base_url, os=os)
    except BootstrapInputError as exc:
        if not edition_features.enabled("cloud_nodes"):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise cloud_node_launch.control_plane_url_refusal(
            f"{exc} — set 'Control plane URL' in "
            "Settings > Infrastructure > Cloud to the address nodes should dial"
        ) from exc


def _validated_new_node_name(
    session: Session,
    node_id: str,
    *,
    org_id: UUID,
    require_dns_label: bool = False,
    host_handle: bool = False,
) -> str:
    """The refusals every `nodes` INSERT owes, in one place: trimmed name, or an HTTPException.

    Shared by all three creation routes because they are properties of the name column: empty (unreadable
    label) and uuid-shaped (`_UUID_SHAPED_NAME_REFUSAL`). A name is a label, never an identity (the id is), so
    a name another node already carries is allowed. `org_id` is the org that will own the row.

    `require_dns_label` (the name is also the systemd and tailnet hostname, `node_bootstrap._NODE_NAME_RE`)
    is hoisted here because `node_bootstrap` applies it only after a real Tailscale key has been minted.
    Imported, not re-spelled. Opt-in: only the two provisioning doors ask; `POST /api/nodes` registers an
    existing machine. The late check in `node_bootstrap` stays as the backstop.

    A name ending `-host` is refused on every door (`hosts.HOST_SUFFIX`): it is the shape of a host's machine
    handle, so no node can take the handle its host will need. `host_handle=True` is the one exception, for
    `app/node_orders.py` writing exactly that handle."""
    name = node_id.strip()
    if not name:
        raise HTTPException(status_code=422, detail="node_id must not be empty")
    if is_uuid_shaped(name):
        raise HTTPException(status_code=422, detail=_UUID_SHAPED_NAME_REFUSAL)
    if not host_handle and hosts.is_host_handle(name):
        raise HTTPException(status_code=422, detail=hosts.HOST_SUFFIX_REFUSAL)
    if require_dns_label and _NODE_NAME_RE.fullmatch(name) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"node_id '{name}' is not a DNS label — a node's name becomes its systemd unit and "
                "its tailnet hostname, so it must be letters, digits and inner hyphens, at most 63 "
                "characters"
            ),
        )
    return name


def _registered_node(
    session: Session,
    user: Optional[AdminUser],
    name: str,
    *,
    cp_base_url: str,
    org_id: UUID,
    charge_nodes_cap: bool = True,
    runs_work: bool = True,
    onboarding_assign: bool = True,
    nodelinks: Optional[Iterable[UUID]] = None,
    cloud: Optional[str] = None,
    fetch_base_url: Optional[str] = None,
    **fields: Any,
) -> tuple[Node, NodeTokenIssuedOut]:
    """The `nodes` row and its credential, in the caller's open transaction and uncommitted.

    Step 1 of `app/cloud_node_launch.py`'s order, shared so no door can forget "no node row without a
    credential". The flush assigns the `uuidv7()` id `_issue_credential` needs.

    `cp_base_url` is required: each door resolves it differently (request origin, or the knob for
    `node_group_reconcile`), and a wrong fallback yields a box that never registers.

    Setdefaults (`enabled` True per `NodeCreateIn`, `jaah_home`; `shared_accounts_enabled` is
    always written false)
    live here, the one door every row comes through; callers may override by naming the field.
    `jaah_home` must be set: NULL makes `node_clone.resolve_clone` answer `""`, so the node gets no
    roster, never observes, and silently reports nothing. Seeded from
    `node_bootstrap.provisioned_jaah_home(os)`, the home a FRESH box is given.

    `org_id` is keyword-only, required, and never defaulted: `trg_nodes_org_id_immutable` makes a wrong
    org unrepairable short of re-enrolling, and operator scope stamps nothing (org-boundary spec §5.2).
    Request doors take it from the body; `node_group_reconcile._launch_one` from `NodeGroup.org_id`. A
    test guards the list of doors.

    The org's Nodes cap (`app/org_limits.py`) is checked here, before the row, so every door refuses a
    full org with the one 409. `charge_nodes_cap=False` is passed for a row the cap does not newly count:
    `node_group_reconcile._launch_one`'s node was already counted as the line's still-launching capacity
    when `desired` was raised (`routes/node_groups.py`), and a Multi-Node Host (the cloud and self-hosted
    doors' `kind="vnode_host"`) is excluded from the count altogether.

    While the org is onboarding (D15) the new node is assigned every project of its org — on this one door,
    so a registration, a launch, a self-hosted create and a group's scale-out all get it. `runs_work=False`
    is a Multi-Node Host's, whose `vnode_hosts` marker its door adds only after this returns.
    `onboarding_assign=False` is a node order's: the order carries the person's explicit project pick
    (`app/node_orders.py::apply_projects`, an empty pick meaning none), which the review step showed them.

    A Multi-Node Host (`runs_work=False`) mounts no NodeLink, so a non-empty `nodelinks` is a 422 for it.
    `nodelinks` is a create door's NodeLink list (`app/nodelinks.py::birth_set` proves it, before the row):
    the node is born attached to exactly those; None or `[]` is none, every door's default. `cloud` is the
    provider key of the cloud the node will run on (None: a self-hosted door), so `birth_set` refuses a
    NodeLink on another cloud's storage."""
    if edition_features.enabled("nodelink"):
        # Deferred: `app.nodelinks` reaches `app.vnodes`, which imports this module.
        from app import nodelinks as nodelinks_service

    if charge_nodes_cap:
        org_limits.require_headroom(session, org_id, "nodes")
    born_with: list[UUID] = []
    if nodelinks is not None and not runs_work and list(nodelinks):
        raise HTTPException(status_code=422, detail=HOST_NODELINKS)
    if nodelinks is not None and edition_features.enabled("nodelink"):  # The community edition has no NodeLink.
        born_with = nodelinks_service.birth_set(
            session, org_id, nodelinks, os=fields.get("os", node_platform.OS_UBUNTU), cloud=cloud
        )
    # True, with the column and `NodeCreateIn` (which argues the alarm tradeoff).
    fields.setdefault("enabled", True)
    # The layout the bootstrap gives a fresh box, imported; the node's own report corrects it.
    fields.setdefault(
        "jaah_home", provisioned_jaah_home(fields.get("os", node_platform.OS_UBUNTU))
    )
    # The retired NodeLink switch: no reader decides anything on it any more (mounts follow attachments, tokens
    # the node's own report), but its column defaults true, so a new node is written false — born with no
    # NodeLink, it must not claim a share.
    fields["shared_accounts_enabled"] = False
    # Assigned, not setdefaulted (see the docstring). An explicit value at the write seam, so
    # `app/tenancy/write_seam.py::stamp_org` refuses a cross-org write (`TenancyWriteRefused`).
    row = Node(name=name, org_id=org_id, **fields)
    # A registration attributes to its operator, not the column's system-actor default. `user=None` has one
    # caller, `app/node_group_reconcile.py`, where the system actor is right; the stamp is skipped
    # because `stamp_actor` deliberately refuses None.
    if user is not None:
        stamp_actor(row, user, created=True)
    session.add(row)
    session.flush()
    if born_with:
        nodelinks_service.attach_at_birth(session, row, born_with, actor_id=user.id if user is not None else None)
    if runs_work and onboarding_assign:
        # Attributed only to a member of the node's own org: an operator's credential cannot serve a tenant's clone.
        auto_assign_as(session, user if user is not None and user.org_id == org_id else None, node_id=row.id)
    return row, _issue_credential(session, row, cp_base_url, fetch_base_url)


def _refuse_a_registered_launch_record(fields: dict[str, Any], actor: AdminUser) -> None:
    """422 when a non-operator's registration names a `_SUPERADMIN_ONLY_INFRA_KEYS` key in `infra`.

    Registration provisions nothing, so it has no launch record or share to write: those come from
    `POST /api/nodes/cloud` and `/api/nodes/self-hosted`, which resolve the credential through the
    caller's seam. Refused outright rather than checked, so a tenant cannot stamp another org's
    credential onto a new row. The operator keeps authoring them (e.g. a terraform-placed machine)."""
    if is_operator_plane(actor.org_id, actor.is_superadmin):
        return
    infra = fields.get("infra")
    named = [key for key in _SUPERADMIN_ONLY_INFRA_KEYS if isinstance(infra, dict) and key in infra]
    if named:
        raise HTTPException(
            status_code=422,
            detail=(
                f"infra.{', infra.'.join(named)} cannot be set when registering a node — launch a cloud "
                "node or create a self-hosted one, which record them."
            ),
        )


@router.post("/api/nodes", response_model=NodeRegisteredOut, status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def create_node(
    body: NodeCreateIn,
    request: Request,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Any:
    """Register a node that already exists: one of three `nodes` creators and the only one that provisions
    nothing (`POST /api/nodes/cloud` launches, `/api/nodes/self-hosted` is hand provisioning); all insert
    through `_registered_node`. No self-report path creates a row: with `enabled` defaulting true, any
    token holder dialling in would otherwise become a dispatchable member without anyone deciding so.

    409 on an existing node_id: registration is not an upsert, so an operator's `enabled`/`role`/`capacity`
    cannot be silently overwritten. Use `PUT` to edit. See `NodeCreateIn` for the `enabled` default.

    Superadmin, because the response carries a working `X-Agent-Token` for the whole agent surface
    (including broker secrets and command dispatch); a lower tier would be an escalation path, and the
    same tier as `reissue_node_token` keeps one door. Cost: a `system:create` holder cannot register.
    A tenant superadmin registers into their own org only (`access_admin.refuse_foreign_org`), and names no
    launch record or share (`_refuse_a_registered_launch_record`).

    Presence is unaffected: the socket row (`app/node_presence.py`) still tracks any dialling node, so `GET /api/nodes`
    shows an unregistered node as present (for callers who see presence). Visible and member are two facts."""
    if edition.IS_CE:
        # The community edition has exactly one node, the one its boot creates (`app/ce_bootstrap.py`).
        return ce_caps.not_in_ce()
    # Before `named_org`, so a tenant naming any org but their own gets one uniform 422 (no existence oracle).
    refuse_foreign_org(body.org_id)
    # A pending org adds no node (`app/org_gate.py`); after `named_org`, so a missing org stays its 422.
    org_id = named_org(session, body.org_id)
    require_org_active(session, org_id)
    node_id = _validated_new_node_name(session, body.node_id, org_id=org_id)

    # `name` is refused, not dropped: a caller sending both believes it declares a node under a name node.env
    # will not present. `name` comes from `node_id`; one spelling.
    if "name" in body.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail=(
                "`name` is not accepted on registration — a node's name IS its `node_id` (the value "
                "/etc/jaah/node.env holds and the daemon presents). Register with `node_id` alone, "
                "then PUT `name` if it really has to differ."
            ),
        )
    # `org_id` is passed by name below; left in `fields` it would be a duplicate keyword.
    fields = body.model_dump(exclude_unset=True, exclude={"node_id", "name", "org_id"})
    node_allocation.drop_on_create(fields)
    # `enabled` rides `_registered_node`'s default; an explicit `enabled: false` still wins.

    # The same refusals the PUT owes, on the door that creates the row: `NodeCreateIn` inherits every
    # editable field, so a PUT-only guard is a boundary on one of two doors. The null arm matters more
    # here: these columns' Python-side defaults run on INSERT, so a sent null was silently replaced with the default (landing a node on the
    # wrong side of the shared-credential switch). The existing functions are called, in `update_node`'s
    # order (null before value).
    _refuse_a_registered_launch_record(fields, user)
    _reject_an_explicit_null_on_a_not_null_column(fields)
    # `draining` is refused outright: inherited from `NodeUpdateIn`, it would reach `Node(**fields)`, yet no
    # registration can describe a drain; `{"draining": true}` would be disabled on the next pass, and with
    # `enabled: false` it is an `IntegrityError` (`ck_nodes_draining_implies_enabled`). Refused, not dropped,
    # as `name` is. Drain after it exists, with the PUT.
    if "draining" in fields:
        raise HTTPException(
            status_code=422,
            detail=(
                "`draining` is not accepted on registration — a drain winds down the work already on a "
                "node, and a node being registered has none. Register it, then PUT "
                "`{\"draining\": true}` when it should stop taking new work."
            ),
        )

    # MicroVM is a later decision with refusals that read the live node (`microvm_mode.blocker`): refused here,
    # as `draining` is, rather than dropped.
    if fields.keys() & set(_MICROVM_FIELDS):
        raise HTTPException(
            status_code=422,
            detail="Register the node first, then turn MicroVM on or set its VM size from its page.",
        )

    # NOT NULL DEFAULT '', so an explicit null is coerced rather than refused by the DB.
    if fields.get("description") is None:
        fields.pop("description", None)
    # The row and its credential in one transaction: the invariant "no node without a credential"
    # (`_registered_node`; the nullable column is only for pre-0162 rows).
    # No origin probe (this route provisions nothing), but the URL is still validated in `_fetch_command`:
    # an `http://` address that would send the token in clear is a 422 naming `cloud_node_cp_base_url`
    # (via `_cloud_node_errors`). So a console at a non-loopback LAN `http://` address must set that knob;
    # loopback stays exempt, so a local `./dc` stack is unaffected.
    with _cloud_node_errors():
        row, credential = _registered_node(
            session,
            user,
            node_id,
            cp_base_url=cloud_node_launch.onboarding_cp_base_url(_request_origin(request)),
            fetch_base_url=console_fetch_origin(request),
            org_id=org_id,
            **fields,
        )
    emit_console_event(session, entity="node", id=row.id, action="upserted")
    session.commit()
    session.refresh(row)

    counts = _counts_map(session, [row.id]).get(
        row.id, NodeCounts(worktrees=0, runners=0, accounts=0)
    )
    detail = _narrow_for_tenant(
        _node_out(
            session,
            str(row.id),
            row,
            jaah_discovery.presence_map(session),
            counts,
            detail=True,
            published=reads._published_release(session, [row]),
        ),
        user,
        principal,
    )
    # The one response that carries a plaintext credential: attached here, so no other node-serving route has
    # a token field to leave empty.
    return NodeRegisteredOut(**detail.model_dump(), credential=credential)
HOST_NODELINKS = "A host runs no work, so it mounts no NodeLinks. Pick NodeLinks for the nodes on it."


@router.post("/api/nodes/{node_id}/token", response_model=NodeTokenIssuedOut, status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def reissue_node_token(
    node_id: str,
    request: Request,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> NodeTokenIssuedOut | JSONResponse:
    """Mint this node a new credential, returning the plaintext once: rotation, recovery and revocation.

    `app/node_credentials.py::issue` overwrites `node_token_hash`, so the old token stops resolving at
    commit; no other row is touched. The node 401s until the new token is installed: run the returned
    command on it.

    Superadmin, unlike this file's `nodes:edit` writes: this hands out a credential for the whole agent
    surface. By uuid (`_node_row`), but not through `_readable_node_or_404`: that door hides existence from scoped
    readers, and a superadmin write gains no oracle from a plain 404. The read is seam-scoped, so a tenant
    superadmin's lookup of another org's node is that same 404."""
    if edition.IS_CE:
        # The community edition's node holds the token its boot wrote (`app/ce_bootstrap.py`).
        return ce_caps.not_in_ce()
    row = _node_row(session, node_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    node_allocation.refuse(row, user, node_allocation.CREDENTIAL_LOCKED)
    require_org_active(session, row.org_id)
    with _cloud_node_errors():
        issued = _issue_credential(
            session, row, cloud_node_launch.onboarding_cp_base_url(_request_origin(request)),
            console_fetch_origin(request),
        )
    # A plain re-issue supersedes an open re-enrol: this command must not mint a key, nor 409 on an
    # expired arming (`routes/agent_bootstrap.py::_reenrolment_is_open`).
    row.tailnet_reenrol_until = None
    emit_console_event(session, entity="node", id=row.id, action="upserted")
    session.commit()
    return issued


@router.post(
    "/api/nodes/{node_id}/tailnet-reenrol", response_model=NodeTailnetReenrolOut, status_code=201
)
@org_admin(Tenancy.CALLER_PLANE)
@session_only
def reenrol_node_on_tailnet(
    node_id: str,
    request: Request,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> NodeTailnetReenrolOut | JSONResponse:
    """Re-issue this node's token AND let its bootstrap mint one tailnet key, for a node already reported in.

    The bootstrap never mints for a node that has reported in (`routes/agent_bootstrap.py`), so moving a
    live fleet to a new tailnet (headscale) needs this. One transaction: the old token dies and the arming
    opens together, so the key reaches only whoever runs the returned command. Single-use (the mint clears
    it) and bounded by the same enrolment window. The door, lookup and 404 are `reissue_node_token`'s.
    Audited by the write-capture middleware (`app/user_activity.py`; the command is redacted by name)."""
    if not edition_features.enabled("tailnet"):
        return ce_caps.not_in_ce()  # The community edition joins no tailnet.
    # Deferred: `routes/agent_bootstrap` imports this package (`node_wire`).
    from app.routes.agent_bootstrap import open_tailnet_reenrolment

    row = _node_row(session, node_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    node_allocation.refuse(row, user, node_allocation.CREDENTIAL_LOCKED)
    require_org_active(session, row.org_id)  # a re-issue, as `reissue_node_token`
    # Refused BEFORE the re-issue: with no tailnet there is no key to hand out, and rotating the token
    # anyway would cut the node off for nothing.
    if cloud_node_launch.tailscale_credential(session) is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "no tailnet credential is configured, so there is no key to re-enrol this node with — "
                "set one in Settings > Infrastructure > Cloud first. Nothing was changed."
            ),
        )
    with _cloud_node_errors():
        issued = _issue_credential(
            session, row, cloud_node_launch.onboarding_cp_base_url(_request_origin(request)),
            console_fetch_origin(request),
        )
    until = open_tailnet_reenrolment(row)
    emit_console_event(session, entity="node", id=row.id, action="upserted")
    session.commit()
    log.info("nodes: tailnet re-enrolment opened for node=%s until=%s", row.name, until.isoformat())
    return NodeTailnetReenrolOut(**issued.model_dump(), tailnet_reenrol_until=until.isoformat())
