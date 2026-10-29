"""What the CP wants a node to run: release, autoupdate, runner flags, port grid and NodeLink mounts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features, node_autoupdate, node_credentials, nodelink, platform_release, port_grid
from app.operator_settings import (
    catalog as settings_catalog,
    errors as settings_errors,
    reads as settings_reads,
)
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import get_session
from app.version import PROCESS_STARTED_AT, stamped_release
from app.models.nodes import Node
from app.routes.agent.auth import _check_token
from app.routes.agent.pool_tick import PoolTickGrid

if edition_features.enabled("nodelink"):
    from app import nodelinks

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ── The node self-token trade route is deleted and must not return ──────────────────────────────
# Re-credentialing a live node is the audited superadmin act (`POST /api/nodes/{node_id}/token`), and
# registration mints in the row's transaction. A route trading one node credential for another's would
# be a privilege escalation (so it is deleted, not deprecated).


# ── The desired agent release: THE CONTROL PLANE'S OWN VERSION ────────────────
# No stored value and no writer: the CP and the `jaah` .deb ship from one commit.
# On the node's convergence path (`/usr/libexec/jaahd-update`), so
# `version` and a JSON `null` body are a wire contract for a POSIX-sh parser: null, never "0.0.0".


class ReleaseDesiredOut(BaseModel):
    version: str
    # When this control plane started serving `version` (`app.version.PROCESS_STARTED_AT`) — a CP's
    # version changes only when a new task starts, so that IS the change time.
    updated_at: datetime
    # Seconds since `updated_at`, from the same process: lets the STALE_RELEASE alarm tell "just published"
    # from "behind for a while".
    updated_age: float


@router.get("/port-grid", response_model=PoolTickGrid)
@infra(Tenancy.ORG_SCOPED)
def get_port_grid(
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> PoolTickGrid:
    """The deterministic port grid, for a caller that is not running a pool tick.

    Consumers: the pool-ports audit tool (audit/reband) and `apps/jaahd/console_direct.py` (refuse a
    console port inside a pool window). Same model as `PoolTickOut.grid`.

    No node scope but an org scope: bands collide only on one host, and a host belongs to one org for life
    (`nodes.org_id`), so per-org disjointness is enough (spec §4.4). Hence `ORG_SCOPED`; for `INFRA` the
    scope is decided by `_read_scope_for` off the arm, and the org comes only from the credential.

    An org-less caller (CP loopback, dev bypass) gets the operator's grid with every var tier 2 until some
    org overrides a port scalar (Orgs > <org> > Settings), after which `operator_settings` refuses the read
    and this answers 409: the grid is per org and the credential proves none. No node can reach that case.
    An empty `range_start_by_var` is legitimate (every var tier 2), not a failed fetch.
    """
    try:
        return PoolTickGrid(**port_grid.grid(session, caller.org_id).wire())
    except settings_errors.OperatorSettingsScopeError:
        raise HTTPException(
            status_code=409,
            detail="An organization overrides the port grid, so it is per organization; this credential names none.",
        ) from None


@router.get("/release-desired", response_model=Optional[ReleaseDesiredOut])
@infra(Tenancy.OPERATOR)
def get_release_desired(
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> Optional[ReleaseDesiredOut]:
    """The release the calling node is meant to be running: this control plane's own version, or for a
    macOS/Windows node the newest one whose bundle is published (`app/platform_release.py`).

    Null when there is nothing the node can install (an unstamped build, or no bundle for its platform).
    The node comes only from the credential, and only its own `os` is read (which bundle it installs), so
    the subject stays the fleet's release; a credential that names no node is answered as Linux is.
    """
    os = session.scalar(select(Node.os).where(Node.id == caller.node_id)) if caller.node_id else None
    desired = cp_release_desired()
    if desired is None:
        return None
    version = platform_release.for_os(os, desired.version).version
    if version is None:
        return None
    return desired.model_copy(update={"version": version})


def cp_release_desired() -> Optional[ReleaseDesiredOut]:
    """The release every Linux node is meant to be running: the version this control plane is itself running.

    No writer: CP and .deb ship from one commit, so this is `TOOL_VERSION`. `updated_at` is process start
    (keeps the STALE_RELEASE grace window working); `updated_age` uses the same clock, not the DB's, so the
    age cannot go negative. Null for an unstamped build (`0.0.0`). The fleet-wide read the Nodes pages and
    the liveness sweep place generations against, through `platform_release.for_os` per node.
    """
    version = stamped_release()
    if version is None:
        return None
    started = PROCESS_STARTED_AT
    return ReleaseDesiredOut(
        version=version,
        updated_at=started,
        updated_age=(datetime.now(timezone.utc) - started).total_seconds(),
    )


class AutoupdateOut(BaseModel):
    # Whether the calling node installs new releases on its own, and where that came from
    # (`app/node_autoupdate.py`: `node`, `host` or `settings`).
    enabled: bool
    source: str


@router.get("/autoupdate", response_model=AutoupdateOut)
@infra(Tenancy.ORG_SCOPED)
def get_autoupdate(
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> AutoupdateOut:
    """The calling node's auto-update switch: Settings, overridden by its host or itself.

    Asked by the daemon before it requests a converge and by each root updater (the .deb's `jaahd-update`,
    the macOS and Windows ports) before it installs anything, so a Settings edit applies on the next pass.
    Fenced to the node the credential proves, never a named one; a credential proving no node reads Settings.
    """
    resolved = node_autoupdate.for_node(session, caller.node_id)
    return AutoupdateOut(enabled=resolved.enabled, source=resolved.source)


# ── The four runner-behaviour kill switches, as a node reads them (R12) ──────────────────────────────


class RunnerFlagsOut(BaseModel):
    """The runner-behaviour switches, and nothing else on this wire.

    Named fields, not a map, so the node-facing surface stays enumerable.
    A test asserts the field set equals
    `operator_settings.RUNNER_FLAG_KEYS`, since a fail-open node would hide a renamed knob.
    """

    runner_no_poll_gate: bool
    runner_read_once: bool
    runner_batch_recon: bool
    runner_hook_dedupe: bool
    runner_project_memory: bool


@router.get("/settings/runner-flags", response_model=RunnerFlagsOut)
@infra(Tenancy.OPERATOR)
def get_runner_flags(
    _: None = Depends(_check_token),
) -> RunnerFlagsOut:
    """The runner-behaviour kill switches, resolved, for the runner-flags tool on a node.

    Read-only, no new credential (`_check_token`), but reviewed as an auth-surface addition.
    `Tenancy.OPERATOR`: every one is an operator-tier fleet value
    (`test_authz_policy._INFRA_OPERATOR_PLANE_ALLOWLIST`; `operator_settings.reads._refuse_unscoped_read`). No
    cache header: `operator_settings` has a 3s cache and the node caches on disk.
    """
    return RunnerFlagsOut(
        **{key: settings_reads.get_bool(key) for key in settings_catalog.RUNNER_FLAG_KEYS}
    )


# ── NodeLink: the store this node should mount ───────────────────────────────────────────────────────
# Read by the node's root mount unit (`jaah-shared-mount converge`) when the CP sends
# `nodelink_converge` (`app/nodelink.py`). The node is derived from the credential, as for the runner pool.


class NodeLinkDesiredOut(BaseModel):
    """What this node should mount on the old single-share wire. `enabled` is tri-state: false = unmount, true
    = mount the ids (its NodeLink at `{jaah-home}/auth/.shared`), null = no opinion (no store of its org is
    ready; the node keeps its own config). The ids are null unless
    `enabled` is true (`nodelink.desired_for_node`); `mount_ip` is also null when the node resolves the EFS
    DNS name."""

    enabled: Optional[bool]
    efs_id: Optional[str] = None
    access_point_id: Optional[str] = None
    region: Optional[str] = None
    mount_ip: Optional[str] = None


@router.get("/nodelink", response_model=NodeLinkDesiredOut)
@infra(Tenancy.ORG_SCOPED)
def nodelink_desired(
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> NodeLinkDesiredOut:
    """Whether this node mounts its NodeLink store, and which. A credential that names no node is a 403
    (no claim is accepted, as `_gha_caller_node_id`); a node with no row answers disabled."""
    if caller.node_id is None:
        raise HTTPException(status_code=403, detail="NodeLink requires a per-node credential")
    node = session.scalar(select(Node).where(Node.id == caller.node_id))
    if node is None:
        return NodeLinkDesiredOut(enabled=False)
    return NodeLinkDesiredOut(**nodelink.desired_for_node(session, node))


class NodeLinkStoreOut(BaseModel):
    efs_id: Optional[str] = None
    region: Optional[str] = None
    mount_ip: Optional[str] = None


class NodeLinkEntryOut(BaseModel):
    id: str
    slug: str
    name: str
    kind: str
    access_point_id: str
    # Absolute, resolved for THIS node.
    path: str


class NodeLinksDesiredOut(BaseModel):
    """Version 1 (`nodelinks.agent_answer`): the NodeLinks of ONE store this node should mount. `store` null
    = no opinion (no store is ready); `store` set with an empty list = unmount every NodeLink."""

    version: int
    store: Optional[NodeLinkStoreOut] = None
    nodelinks: list[NodeLinkEntryOut]


class NodeLinkEntryV2Out(BaseModel):
    id: str
    slug: str
    name: str
    access_point_id: str
    # Absolute, resolved for THIS node.
    path: str
    # The NodeLink's own store, as a daemon that predates `mount` reads it (EFS keys).
    store: NodeLinkStoreOut
    # How to mount it: `{"type": "efs" | "nfs" | "aznfs", …}`, the store's adapter's `mount_spec`.
    # A daemon fails closed on a type it does not know.
    mount: dict[str, Optional[str]]


class NodeLinksDesiredV2Out(BaseModel):
    """Version 2 (`?version=2`, `nodelinks.agent_answer`): every NodeLink this node should mount, each with its
    own store. `authoritative` false = no opinion (no store is ready); true with an empty list = unmount all."""

    version: int
    authoritative: bool
    nodelinks: list[NodeLinkEntryV2Out]


#: `nodelinks.WIRE_VERSION` and `WIRE_VERSION_STORES`; the community edition, which has no NodeLink, ships neither.
_WIRE_VERSION, _WIRE_VERSION_STORES = (
    (nodelinks.WIRE_VERSION, nodelinks.WIRE_VERSION_STORES) if edition_features.enabled("nodelink") else (1, 2)
)


@router.get("/nodelinks", response_model=Union[NodeLinksDesiredV2Out, NodeLinksDesiredOut])
@infra(Tenancy.ORG_SCOPED)
def nodelinks_desired(
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
    version: int = Query(default=_WIRE_VERSION, ge=1),
) -> Union[NodeLinksDesiredV2Out, NodeLinksDesiredOut]:
    """This node's NodeLinks — every one attached to it, of its own org only. Without `?version=2` the
    single-store answer older packages read. A credential that names no node is a 403, as `/nodelink`; a
    node with no row is served nothing."""
    if caller.node_id is None:
        raise HTTPException(status_code=403, detail="NodeLink requires a per-node credential")
    if not edition_features.enabled("nodelink"):
        # No NodeLink: no opinion, as for an org with no store ready, so nothing the node mounts is touched.
        if version >= _WIRE_VERSION_STORES:
            return NodeLinksDesiredV2Out(version=_WIRE_VERSION_STORES, authoritative=False, nodelinks=[])
        return NodeLinksDesiredOut(version=_WIRE_VERSION, store=None, nodelinks=[])
    node = session.scalar(select(Node).where(Node.id == caller.node_id))
    answer = nodelinks.agent_answer(session, node, version=version)
    if answer["version"] >= nodelinks.WIRE_VERSION_STORES:
        return NodeLinksDesiredV2Out(**answer)
    return NodeLinksDesiredOut(**answer)
