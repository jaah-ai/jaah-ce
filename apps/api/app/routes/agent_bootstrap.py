"""The self-hosted bootstrap, served: the far end of the one line an operator pastes on a bare box.

The token is the identity (`_check_token` resolves it to its node row) and the only source of its own
plaintext (the CP stores `sha256(token)`), hence node-token rather than session auth.

The response is a secret: the token plus, on first enrolment or an operator's re-enrol, a single-use tailnet
auth key — a real grant for another system, so rationed (`_may_mint_a_tailnet_key`;
`app/durable/bootstrap_key.py` replays one key per key lifetime). `no-store`, never logged, repeatable so a failed enrolment re-runs the same line.

Internet-reachable, alone on the machine surface (`app/node_bootstrap.py::BOOTSTRAP_PATH`,
`app/auth.py::SESSIONLESS_NODE_TOKEN_PATHS`): the `_check_token` dependency is all that guards it. So a Mac's
installer, not yet on the tailnet, fetches its platform bundle through it too (`?dist=`), not a second public path.
"""
from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import edition_features, ce_caps, node_credentials, node_platform
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import get_session
from app.events import emit_console_event
from app.models.nodes import Node, NodeOrder
from app.entity_shape import get_or_404
from app.node_bootstrap import (
    BOOTSTRAP_FETCHED_KEY,
    BOOTSTRAP_PATH,
    FORMER_NODE_PARAM,
    MACOS_PLATFORM,
    NATIVE_BODIES,
    BootstrapInputError,
    BootstrapInputs,
)
from app.routes import agent_apt, agent_dist
# The one authenticator on `/api/agent/*`; a second would be invisible to the policy test.
from app.routes.agent.auth import _check_token
# Origin and `cloud_node_launch` error mapping, reused so they keep their single home.
from app.routes.nodes.node_wire import _cloud_node_errors, _request_origin

if edition_features.enabled("cloud_nodes"):
    from app import cloud_node_launch, vnodes

log = logging.getLogger("agent-bootstrap")

router = APIRouter()

# How long a credential may still enrol a machine, from issue. `mint_tailnet` bounds which nodes; this
# bounds for how long: `Node.last_seen` is never reset, so without it every abandoned registration stays
# mintable forever, and an enrolled tailnet device survives token re-issue and row deletion.
# A day covers Register-to-machine; re-issuing the token restarts it.
ENROLMENT_WINDOW = timedelta(hours=24)

# `?dist=<filename>`: one dist object for this node's own platform, over this public path — the installer runs
# before the box is on the tailnet, where `agent_dist`'s route lives.
DIST_PARAM = "dist"
# The dist platform each OS may fetch this way; macOS only (Windows' installer cannot use it yet).
_DIST_PLATFORM = {node_platform.OS_MACOS: MACOS_PLATFORM}


@router.get(BOOTSTRAP_PATH, response_class=PlainTextResponse)
@infra(Tenancy.ORG_SCOPED)
def serve_node_bootstrap(
    request: Request,
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
    former_node: uuid.UUID | None = Query(None, alias=FORMER_NODE_PARAM),
    dist: str | None = Query(None, alias=DIST_PARAM),
) -> Response:
    """Render this node's own bootstrap, for `curl … | sudo bash`.

    With `?former_node=<uuid>`, answer instead whether that node still exists (`present`/`absent`): the
    served script asks when the box's node.env names another node, so a machine whose node was deleted
    re-enrols as this one rather than refusing (`app/node_bootstrap.py`). Hard delete (`delete_node`)
    means no row = gone; any org's row is `present` (`_node_exists_anywhere`).

    With `?dist=<filename>`, redirect instead (307) to a presigned GET for that file of this node's platform
    bundle: `agent_dist`'s allowlist and presign, so the same 404 for any name it would refuse.

    The derived-identity check is the whole authorization: an arm with no node identity (CP loopback,
    local-dev bypass) has nothing to be served, so `is_derived` refuses rather than falls back.
    """
    if not edition_features.enabled("cloud_nodes"):
        # An edition without it renders no bootstrap: the community edition's one node is provisioned by its boot
        # (`app/ce_bootstrap.py`), never by this line.
        return ce_caps.not_in_ce()
    if not caller.is_derived:
        raise HTTPException(
            status_code=403,
            detail=(
                "this endpoint serves one node its own bootstrap, so it needs a per-node token — "
                "the credential presented names no node. Register the node (or re-issue its token) "
                "and use the command that response carries."
            ),
        )
    # Credential resolved but the row was deleted between copy and paste.
    row = get_or_404(session, Node, caller.node_id, detail="this node no longer exists")

    if dist is not None:
        return _dist_redirect(request, row, caller)

    if former_node is not None:
        answer = "present" if _node_exists_anywhere(session, former_node) else "absent"
        log.info("agent-bootstrap: node=%s asked about its box's former node: %s", row.name, answer)
        return PlainTextResponse(answer, headers={"Cache-Control": "no-store"})

    # The plaintext comes off the request (the column holds a digest); `_check_token` proved it hashes
    # to this row's credential.
    node_token = request.headers.get("x-agent-token", "")

    # A macOS or Windows node gets its own platform's body, by the OS it was declared as (`nodes.os`).
    native = NATIVE_BODIES.get(row.os)
    if native is not None:
        inputs = BootstrapInputs(
            node_id=str(row.id),
            node_name=row.name,
            node_token=node_token,
            cp_base_url=cloud_node_launch.onboarding_cp_base_url(_request_origin(request)),
            # The public origin this fetch arrived on: a Mac's installer fetches its bundle back through it
            # (`DIST_PARAM`) before it can reach `cp_base_url`'s tailnet-only routes.
            public_base_url=_request_origin(request) if row.os == node_platform.OS_MACOS else None,
        )
        # A Mac joins on its first enrolment and, re-enrolled by an operator, re-joins with `--force-reauth`
        # (`_may_mint_a_native_key` admits a reported-in node only then). A PC joins on its FIRST enrolment
        # only: its body has no forced re-join, so a re-enrolled one would spend the key for nothing.
        mint = (row.os == node_platform.OS_MACOS or row.last_seen is None) and _may_mint_a_native_key(
            session, row, node_token
        )
        if mint:
            with _cloud_node_errors():
                inputs = cloud_node_launch.with_tailnet_join(
                    inputs, tailnet_reenrol=row.last_seen is not None
                )
        # Mapped as Ubuntu's render is: a key's own checks (`_validate`) run only now.
        with _cloud_node_errors():
            try:
                body = native(inputs)
            except BootstrapInputError as exc:
                raise cloud_node_launch.CloudNodeError(str(exc)) from exc
        _spend_reenrolment(session, row, minted=mint)
        _stamp_fetched(session, row)
        log.info("agent-bootstrap: serving the %s bootstrap for node=%s arm=%s", row.os, row.name, caller.arm)
        return PlainTextResponse(
            body,
            media_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    mint_tailnet = _may_mint_a_tailnet_key(session, row, presented_token=node_token)
    with _cloud_node_errors():
        script = cloud_node_launch.self_hosted_bootstrap(
            session,
            node_id=str(row.id),
            org_id=row.org_id,
            node_name=row.name,
            node_token=node_token,
            mint_tailnet=mint_tailnet,
            # Admitted past `last_seen`, so only by an operator's re-enrol: render the forced re-join.
            tailnet_reenrol=mint_tailnet and row.last_seen is not None,
            # The knob wins, else this request's origin, unprobed: reaching this handler already
            # proves the origin serves the machine surface.
            cp_base_url=cloud_node_launch.onboarding_cp_base_url(_request_origin(request)),
            # A Multi-Node Host's re-fetch keeps its marker.
            vnode_host=vnodes.is_host(session, row.id),
        )

    _spend_reenrolment(session, row, minted=mint_tailnet)
    _stamp_fetched(session, row)

    # Name only: the script and its token are credentials.
    log.info("agent-bootstrap: serving the bootstrap for node=%s arm=%s", row.name, caller.arm)
    return PlainTextResponse(
        script,
        # Descriptive only; `curl | bash` never looks.
        media_type="text/x-shellscript",
        # A credential in a response body: never cached.
        headers={"Cache-Control": "no-store"},
    )


def _spend_reenrolment(session: Session, row: Node, *, minted: bool) -> None:
    """The re-enrol is spent: one key per arming. Called only once the render (and so the mint) has succeeded;
    a refusal before it rolls back and leaves it armed. The row lock taken in `_may_mint_a_tailnet_key` is
    held until here, so a concurrent fetch waits and then sees it cleared."""
    if not minted or row.tailnet_reenrol_until is None:
        return
    row.tailnet_reenrol_until = None
    session.commit()
    log.info("agent-bootstrap: re-enrolment spent for node=%s", row.name)


def _stamp_fetched(session: Session, row: Node) -> None:
    """The install command ran: its machine fetched the script. A host page waiting on a machine that never did says
    the command has not run (`BOOTSTRAP_FETCHED_KEY`) — e.g. EC2 user-data with no `#!` line, which cloud-init skips.
    The order awaiting this machine is told at once: its progress dialog swaps the command for the install's bar."""
    row.infra = {**(row.infra or {}), BOOTSTRAP_FETCHED_KEY: datetime.now(timezone.utc).isoformat()}
    for order in session.scalars(select(NodeOrder).where(
        NodeOrder.host_node_id == row.id, NodeOrder.stage == "waiting_for_machine",
    )):
        emit_console_event(session, entity="node_order", id=order.id, action="upserted", org_id=order.org_id)
    session.commit()


def _dist_redirect(request: Request, row: Node, caller: node_credentials.AgentCaller) -> RedirectResponse:
    """The `?dist=` answer: 409 for an OS with no dist fetch here, 404 for a name `agent_dist` would refuse."""
    platform = _DIST_PLATFORM.get(row.os)
    if platform is None:
        raise HTTPException(
            status_code=409,
            detail=f"{node_platform.label(row.os)} nodes do not fetch their bundle through this path.",
        )
    # The raw query value, not the decoded one: a version's literal `+` would decode to a space, and passing it
    # as `raw_path` keeps `agent_dist`'s refusal of any `%`.
    raw = next((v for k, _, v in (p.partition("=") for p in request.url.query.split("&")) if k == DIST_PARAM), "")
    key = agent_dist._dist_key(platform, raw, raw)
    if key is None:
        raise HTTPException(status_code=404, detail="no such object")
    return agent_apt.presigned_redirect(key, channel="dist", caller=caller)


def _node_exists_anywhere(session: Session, node_id: uuid.UUID) -> bool:
    """Is `node_id` a node's — in ANY org? The box's former node, read off its own node.env.

    UNSCOPED BY DESIGN (`test_tenancy_bypass_sites.py`, `exact-value-probe`): scoped to the caller's org,
    another tenant's LIVE node would miss, read `absent`, and the script would re-provision that tenant's
    machine for this one. Answers a bool from a value the caller sent; a v4 id is not guessable."""
    return session.scalar(
        text("SELECT 1 FROM nodes WHERE id = :id LIMIT 1"), {"id": node_id}
    ) is not None


def _may_mint_a_native_key(session: Session, row: Node, presented_token: str) -> bool:
    """`_may_mint_a_tailnet_key` for a macOS or Windows body, where a closed enrolment window means no key
    rather than a 409: those bodies check the tailnet themselves, so a machine off it is refused in preflight
    while one already on it (a PC re-run after the restart WSL 2 needs) installs."""
    try:
        return _may_mint_a_tailnet_key(session, row, presented_token=presented_token)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return False


def _may_mint_a_tailnet_key(session: Session, row: Node, *, presented_token: str = "") -> bool:
    """Whether this fetch may hand out a tailnet auth key, the one thing here the caller did not have.

    A key is a credential the CP cannot revoke: a device enrolled with it keeps its tailnet tags (`tag:jaah-node`)
    after token re-issue and row deletion. Checked in order:

    * Already on the tailnet? `Node.last_seen` (sole writer `app/jaah_discovery.py`, on bus-online)
      non-null means no key — unless an operator re-enrolled it (`_reenrolment_is_open`). Not
      `node_token_used_at`, which this very request stamps.
    * No tailnet credential configured? Then no key exists; do not refuse over it.
    * Credential still inside `ENROLMENT_WINDOW`? A NULL issue time is refused under its own message.

    The third refuses (409 naming the remedy) rather than returning False: a script without a tailnet
    join would provision a box that silently never becomes a node.
    """
    if row.last_seen is not None:
        return _reenrolment_is_open(session, row, presented_token)
    if cloud_node_launch.tailscale_credential(session) is None:
        return False
    issued = row.node_token_issued_at
    # NULL issue time (a row predating the column) cannot be dated; refused, since this branch mints a
    # durable credential, with a message that does not claim expiry.
    if issued is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "this control plane cannot tell when this node's credential was issued, so it will "
                "not mint it a tailnet key. Re-issue its token in the console (Nodes -> the node's "
                "... menu -> Re-issue token) and run the command that response carries."
            ),
        )
    if issued.tzinfo is None:  # a column written before the tz-aware default; treat as UTC
        issued = issued.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - issued <= ENROLMENT_WINDOW:
        return True
    raise HTTPException(
        status_code=409,
        detail=(
            "this node's enrolment window has closed — its credential was issued more than "
            f"{int(ENROLMENT_WINDOW.total_seconds() // 3600)}h ago and the node has never reported "
            "in, so this control plane will not mint it a tailnet key. Re-issue its token in the "
            "console (Nodes -> the node's ... menu -> Re-issue token) and run the command that "
            "response carries; that is the same act as revoking the credential you are holding."
        ),
    )


def open_tailnet_reenrolment(row: Node) -> datetime:
    """Arm `row` for one more tailnet key, for the same `ENROLMENT_WINDOW` a fresh registration gets.

    The caller (`routes/nodes/register.py::reenrol_node_on_tailnet`) re-issues the token in the same transaction, so
    the key can only reach whoever runs the command that response carries. Does not commit."""
    row.tailnet_reenrol_until = datetime.now(timezone.utc) + ENROLMENT_WINDOW
    return row.tailnet_reenrol_until


def _reenrolment_is_open(session: Session, row: Node, presented_token: str) -> bool:
    """Whether an operator's re-enrol lets this reported-in node mint one key now.

    Not armed: no key (the default for every live node). Armed and open: locks the row until the handler's
    commit, so two concurrent fetches cannot both spend one arming. Armed but expired: 409 rather than a
    script with no join, which would rotate the token and silently leave the node off the new tailnet.

    The presented token is re-proved against the LOCKED row: `_check_token` resolved it a statement earlier
    under READ COMMITTED, so a fetch with the OLD token racing the re-enrol's commit could pass that check
    and then read the fresh arming. Only the token the re-enrol issued may spend it."""
    if row.tailnet_reenrol_until is None:
        return False
    if cloud_node_launch.tailscale_credential(session) is None:
        return False
    session.refresh(row, with_for_update=True)
    if row.node_token_hash is None or not hmac.compare_digest(
        row.node_token_hash, node_credentials.digest(presented_token)
    ):
        raise HTTPException(status_code=401, detail="invalid agent token")
    until = row.tailnet_reenrol_until
    if until is None:  # a concurrent fetch spent it while this one waited for the lock
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < until:
        return True
    raise HTTPException(
        status_code=409,
        detail=(
            "this node's re-enrolment has expired — it was opened more than "
            f"{int(ENROLMENT_WINDOW.total_seconds() // 3600)}h ago, so this control plane will not mint "
            "it a tailnet key. Re-enrol it again in the console (Nodes -> the node -> Re-enrol) and run "
            "the command that response carries."
        ),
    )
