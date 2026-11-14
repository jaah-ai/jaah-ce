"""Custom connectors' API: an org's own REST APIs and MCP servers (`app/integrations/custom.py` holds the rules).

* `GET /api/integrations/custom[/{id}]` — every member reads their org's connectors (the gallery shows them).
* `POST`, `PUT`, `DELETE /api/integrations/custom[/{id}]` — Org Owners and Admins only (`@org_admin`).
* `POST /api/integrations/custom/{id}/connections` — anyone who may add a secret connects: a `credentials` row of
  the generic type the connector maps to, its config carrying `custom_connector_id`.
* `POST /api/integrations/custom/{id}/test` — the connection's Test against the connector's `base_url + test_path`,
  dialled only at vetted public addresses, so a tenant may run it (unlike `POST /api/credentials/test`).

Every query names the caller's own org (`_org`) on top of the tenancy seam: an operator's READ is fleet-wide on the
seam, and a custom connector is still only ever its own org's. Another org's id is a 404, never a 403.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import data_access_events, db_async
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import feature, org_admin
from app.authz.object_access import stamp_owner_on_create
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.authz.visibility import scoped_credential_select_for
from app.credentials import health as credential_health
from app.credential_crypto import SealedSecret
from app.credentials.registry import CREDENTIAL_TYPES, sealing_destination
from app.credentials.testers import TestResult, new_deadline, nodevault_untestable_result
from app.db_async import DbDep, on_session
from app.entity_shape import commit_row, from_row, get_or_404, or_404, refused_as
from app.integrations import catalog
from app.integrations import custom
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.integrations import CustomConnector
from app.routes.credentials.connection_test import CredentialTestOut, _test_out
from app.routes.credentials.credential_wire import (
    _SealScope,
    _audit,
    _credential_ownership,
    _merged_config,
    _optional_uuid,
    _refuse_duplicates_in_home,
    _require_declared_fields,
    _scrubbed_write,
    _validated_name,
)
from app.routes.credentials.guards import _scoped_row_or_404
from app.routes.credentials.writes import _id_is_minted
from app.tenancy.seam import bound_org

router = APIRouter(prefix="/api/integrations/custom", tags=["integrations"])

_VIEW_KEY = permission_key("credentials", "view")
_EDIT_KEY = permission_key("credentials", "edit")
_NOT_FOUND = "no such custom connector"
_SLUG_TAKEN = "A custom connector with this slug already exists in this organization."
#: The generic types a connection may be (`custom.credential_type_for`).
CONNECTION_TYPES = ("generic_api_key", "generic_login", "oauth2_client", "mcp_server")

Kind = Literal["rest", "mcp"]
Scheme = Literal["bearer", "header_key", "basic", "oauth2_client", "none"]


class CustomConnectorIn(BaseModel):
    """A whole definition (PUT replaces it). `slug` is derived from `name` when blank and fixed once created."""

    name: str = Field(max_length=200)
    slug: str = Field(default="", max_length=40)
    description: str = Field(default="", max_length=2000)
    kind: Kind = "rest"
    base_url: str = Field(max_length=2048)
    auth_scheme: Scheme
    header_name: Optional[str] = Field(default=None, max_length=64)
    value_prefix: Optional[str] = Field(default=None, max_length=32)
    oauth_authorize_url: Optional[str] = Field(default=None, max_length=2048)
    oauth_token_url: Optional[str] = Field(default=None, max_length=2048)
    oauth_scope: Optional[str] = Field(default=None, max_length=1024)
    test_path: Optional[str] = Field(default=None, max_length=512)
    mcp_transport: Optional[Literal["streamable_http", "sse"]] = None


class ConnectionOut(BaseModel):
    id: UUID
    name: str
    owner: Literal["org", "personal"]
    last_tested_at: Optional[datetime]
    last_test_ok: Optional[bool]
    last_test_message: Optional[str]
    needs_reconnect: bool
    # Whether the caller may Test (and edit) it: their own row, or `credentials:edit` on an org row.
    editable: bool


class CustomConnectorOut(BaseModel):
    """One connector, with the connections to it the caller may see. `credential_type` is what a connection is
    saved as; `secret_inputs` what Connect asks for (`{input: required}`)."""

    id: UUID
    slug: str
    name: str
    description: str
    kind: Kind
    base_url: str
    auth_scheme: Scheme
    header_name: Optional[str]
    value_prefix: Optional[str]
    oauth_authorize_url: Optional[str]
    oauth_token_url: Optional[str]
    oauth_scope: Optional[str]
    test_path: Optional[str]
    mcp_transport: Optional[str]
    credential_type: str
    secret_inputs: dict[str, bool]
    # What a browser seals a connection's secrets to (`lib/credentialSeal.ts`, the registry's `sealing_destination`).
    sealing_destination: str
    created_at: datetime
    updated_at: datetime
    connections: list[ConnectionOut] = Field(default_factory=list)


#: A new connection's body: its name, owner (`org` needs an Org Owner/Admin), the id the browser minted to seal
#: under, and the secret inputs the scheme takes, each a browser-sealed envelope (`lib/credentialSeal.ts`) or, from
#: a script, a string. Parsed by hand, never a pydantic model, so a refusal can never echo a submitted secret (422).
_CONNECTION_BODY_KEYS = frozenset({"name", "owner", "id"})


def _parsed_connection_body(raw: Any) -> tuple[str, str, Optional[UUID], dict[str, Any]]:
    """`(name, owner, client id, inputs)` or a value-free 400."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    name, owner = raw.get("name"), raw.get("owner", "personal")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="name is required")
    if owner not in ("org", "personal"):
        raise HTTPException(status_code=400, detail="owner must be org or personal")
    client_id = _optional_uuid(raw.get("id"), "id must be a uuid")
    inputs: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _CONNECTION_BODY_KEYS:
            continue
        if key not in custom.ALL_INPUTS:
            raise HTTPException(status_code=400, detail=f"unknown field '{key[:64]}'")
        if value is not None and not isinstance(value, (str, dict)):
            raise HTTPException(status_code=400, detail=f"{key} must be a string or a sealed value")
        inputs[key] = value
    return name, owner, client_id, inputs


class ConnectionTestIn(BaseModel):
    credential_id: UUID


# ── Reads shared with the gallery ─────────────────────────────────────────────────────────────────────


def caller_org(user: AdminUser) -> UUID:
    """The org a custom connector belongs to for this caller: the request's bound org (a tenant, or an operator
    acting as one), else the caller's own. Never fleet-wide."""
    return or_404(bound_org() or user.org_id, detail=_NOT_FOUND)  # every signed-in user has an org


def org_connectors(session: Session, org_id: UUID) -> list[CustomConnector]:
    return list(session.scalars(
        select(CustomConnector).where(CustomConnector.org_id == org_id).order_by(CustomConnector.name)
    ))


def visible_connections(session: Session, principal: Principal | None, org_id: UUID) -> dict[UUID, list[Credential]]:
    """`{connector id: [rows]}` over the generic-type rows the caller may see in `org_id` that name a connector.
    The link lives in the (sealed) config, so it is read off each row's opened tier 1 - no secret is opened."""
    rows = session.scalars(
        scoped_credential_select_for(principal, _VIEW_KEY).where(
            Credential.type.in_(CONNECTION_TYPES), Credential.org_id == org_id,
        )
    )
    out: dict[UUID, list[Credential]] = {}
    for row in rows:
        link = custom.connector_id_of(row)
        if link is not None:
            out.setdefault(link, []).append(row)
    return out


def editable_ids(session: Session, principal: Principal | None, org_id: UUID) -> set[UUID]:
    """The ids of the generic-type rows in `org_id` the caller may edit, so Test is offered only on those."""
    return set(session.scalars(
        scoped_credential_select_for(principal, _EDIT_KEY)
        .where(Credential.type.in_(CONNECTION_TYPES), Credential.org_id == org_id)
        .with_only_columns(Credential.id)
    ))


def _connection_out(row: Credential, *, editable: bool) -> ConnectionOut:
    return from_row(
        ConnectionOut, row,
        owner="personal" if row.owner_id is not None else "org",
        needs_reconnect=row.needs_reconnect_at is not None, editable=editable,
    )


def _out(
    connector: CustomConnector, connections: list[Credential] = (), editable: frozenset[UUID] | set[UUID] = frozenset(),  # type: ignore[assignment]
) -> CustomConnectorOut:
    return from_row(
        CustomConnectorOut, connector,
        description=connector.description or "",
        credential_type=custom.credential_type_for(connector),
        secret_inputs=dict(custom.SECRET_INPUTS[(connector.kind, connector.auth_scheme)]),
        sealing_destination=sealing_destination(custom.credential_type_for(connector), custom.skeleton_config(connector)),
        connections=[_connection_out(row, editable=row.id in editable)
                     for row in sorted(connections, key=lambda r: r.name.lower())],
    )


def _connector_or_404(session: Session, connector_id: UUID, org_id: UUID, *, lock: bool = False) -> CustomConnector:
    return get_or_404(session, CustomConnector, connector_id, detail=_NOT_FOUND,
                      where=(CustomConnector.org_id == org_id,), lock=lock)


def _apply(session: Session, row: CustomConnector, body: CustomConnectorIn) -> None:
    """Write a validated definition onto `row`; 400 naming the first problem."""
    with refused_as(400, custom.ConnectorError):
        fields = custom.checked_fields(
            kind=body.kind, base_url=body.base_url, auth_scheme=body.auth_scheme, header_name=body.header_name,
            value_prefix=body.value_prefix, oauth_authorize_url=body.oauth_authorize_url,
            oauth_token_url=body.oauth_token_url, oauth_scope=body.oauth_scope, test_path=body.test_path,
            mcp_transport=body.mcp_transport,
        )
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    row.name = name
    row.description = body.description.strip()
    for key, value in vars(fields).items():
        setattr(row, key, value)


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[CustomConnectorOut])
@feature("credentials", "view", own_credential=True)
@on_session
def list_custom_connectors(session: Session, caller: AsyncCaller = Depends(current_caller)) -> list[CustomConnectorOut]:
    """The caller's org's connectors, by name, each with the connections to it the caller may see."""
    org = caller_org(caller.actor)
    links = visible_connections(session, caller.principal, org)
    editable = editable_ids(session, caller.principal, org)
    return [_out(row, links.get(row.id, []), editable) for row in org_connectors(session, org)]


@router.get("/{connector_id}", response_model=CustomConnectorOut)
@feature("credentials", "view", own_credential=True)
@on_session
def get_custom_connector(
    session: Session, connector_id: UUID, caller: AsyncCaller = Depends(current_caller),
) -> CustomConnectorOut:
    org = caller_org(caller.actor)
    row = _connector_or_404(session, connector_id, org)
    return _out(row, visible_connections(session, caller.principal, org).get(row.id, []),
                editable_ids(session, caller.principal, org))


@router.post("", response_model=CustomConnectorOut, status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
# Off the loop: the base URL's host is resolved to vet it (`custom.public_https_url`).
@on_session
def create_custom_connector(
    session: Session, body: CustomConnectorIn, user: AdminUser = Depends(require_user),
) -> CustomConnectorOut:
    """Define a connector in the caller's org (Org Owner/Admin). The base URL must be https and public."""
    org = caller_org(user)
    with refused_as(400, custom.ConnectorError):
        slug = custom.checked_slug(
            body.slug.strip() or custom.slug_from_name(body.name), frozenset(catalog.INTEGRATIONS)
        )
    row = CustomConnector(org_id=org, slug=slug)
    _apply(session, row, body)
    stamp_actor(row, user, created=True)
    session.add(row)
    return _out(commit_row(session, row, taken=_SLUG_TAKEN))


@router.put("/{connector_id}", response_model=CustomConnectorOut)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def update_custom_connector(
    session: Session, connector_id: UUID, body: CustomConnectorIn, caller: AsyncCaller = Depends(current_caller),
) -> CustomConnectorOut:
    """Replace a definition (its slug is fixed). While any connection uses it, its kind, scheme, addresses, header and
    prefix are fixed too (409); the name, description, test path and scope may change."""
    org = caller_org(caller.actor)
    row = _connector_or_404(session, connector_id, org, lock=True)
    if body.slug.strip() and body.slug.strip() != row.slug:
        raise HTTPException(status_code=400, detail="slug cannot change once the connector exists")
    links = visible_connections(session, caller.principal, org).get(row.id, [])
    before = _pinned(row)
    _apply(session, row, body)
    if _pinned(row) != before and _any_connection(session, org, row.id):
        # A connection's secrets were given for THIS host and scheme (and are sealed to its address): moving
        # them would make Test, or a runner, send a member's secret wherever an admin points it next.
        raise HTTPException(
            status_code=409,
            detail="The kind, auth scheme, addresses and header cannot change while connections use this connector - "
                   "delete them first, or define a new connector.",
        )
    stamp_actor(row, caller.actor)
    return _out(commit_row(session, row), links, editable_ids(session, caller.principal, org))


def _pinned(row: CustomConnector) -> tuple:
    """What a connection's secret was given for and how it is sent (each connection holds its own copy of the header
    and prefix): fixed while any connection exists."""
    return (row.kind, row.auth_scheme, row.base_url, row.oauth_authorize_url, row.oauth_token_url, row.header_name,
            row.value_prefix)


def _any_connection(session: Session, org_id: UUID, connector_id: UUID) -> bool:
    """Whether any row in the org (visible to the caller or not) names the connector."""
    rows = session.scalars(
        select(Credential).where(Credential.type.in_(CONNECTION_TYPES), Credential.org_id == org_id)
    )
    return any(custom.connector_id_of(row) == connector_id for row in rows)


@router.delete("/{connector_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def delete_custom_connector(
    session: Session, connector_id: UUID, user: AdminUser = Depends(require_user),
) -> Response:
    """Delete a definition. Refused (409) while any connection names it: a connection is a secret its owner deletes
    under Secrets, so deleting the connector never deletes someone's secret."""
    org = caller_org(user)
    row = _connector_or_404(session, connector_id, org, lock=True)
    if _any_connection(session, org, row.id):
        raise HTTPException(
            status_code=409, detail="Connections still use this connector - delete them under Secrets first.",
        )
    session.delete(row)
    session.commit()
    return Response(status_code=204)


@router.post("/{connector_id}/connections", response_model=ConnectionOut, status_code=201)
# Creating a credential: `POST /api/credentials`'s own declaration.
@feature("credentials", "create", own_credential=True)
@on_session
def create_custom_connection(
    session: Session, connector_id: UUID, raw_body: Any = Body(default=None), caller: AsyncCaller = Depends(current_caller),
) -> ConnectionOut:
    """Connect to a connector: a new credentials row of its generic type, validated and sealed exactly as
    `POST /api/credentials` does (a browser-sealed secret is bound to the body's `id` and the connector's address,
    `sealing_destination` on the connector), its config naming the connector. An OAuth2 connector's row is then
    Connected with `POST /api/credentials/connect/start/{id}`."""
    name_in, owner, client_id, inputs = _parsed_connection_body(raw_body)

    org = caller_org(caller.actor)
    # Locked like PUT/DELETE: a concurrent edit cannot move the connector between this read and the insert.
    connector = _connector_or_404(session, connector_id, org, lock=True)
    spec = CREDENTIAL_TYPES[custom.credential_type_for(connector)]
    name = _validated_name(name_in)
    if client_id is not None and _id_is_minted(session, client_id):
        raise HTTPException(status_code=409, detail="a credential with this id already exists")
    with refused_as(400, custom.ConnectorError):
        posted = custom.connection_config(connector, inputs)
    config = _merged_config(spec, {}, posted, reset=True, seal=_SealScope(nodevault=False, session=session,
                                                                          org_id=bound_org()))
    _require_declared_fields(spec, config)
    if client_id is None and any(isinstance(value, SealedSecret) for value in config.values()):
        raise HTTPException(status_code=400, detail="id is required when the body carries sealed secrets")
    config[custom.CONNECTOR_ID_KEY] = str(connector.id)
    row = Credential(id=client_id or uuid4(), name=name, type=spec.slug,
                     description=f"Connection to {connector.name}", config=config, org_id=org)
    stamp_actor(row, caller.actor, created=True)
    stamp_owner_on_create(caller.principal, row, _credential_ownership(owner, caller.principal))
    _refuse_duplicates_in_home(session, [(row, name, None)])
    with _scrubbed_write(session, "create"):
        session.add(row)
        session.flush()
        _audit(session, caller.actor, "credential_created",
               f"Connected custom connector {connector.name} as {spec.label} credential {name}", row,
               owner_user_id=row.owner_id)
        session.commit()
        session.refresh(row)
    return _connection_out(row, editable=True)


@router.post("/{connector_id}/test", response_model=CredentialTestOut)
# Test opens the row's secrets and sends them: `PATCH`'s declaration and narrowing (`_scoped_row_or_404` under
# `credentials:edit`), as the row-bound Connect does. Safe for tenants: the destination is vetted and pinned.
@feature("credentials", "edit", own_credential=True)
async def test_custom_connection(
    connector_id: UUID, db: DbDep, body: ConnectionTestIn, caller: AsyncCaller = Depends(current_caller),
) -> CredentialTestOut:
    """Test one connection against its connector; the verdict is stamped on the row (its health)."""
    connector, config, early = await db.run_sync(_opened_connection, connector_id, body.credential_id, caller.actor, caller.principal)
    await db_async.release(db)
    result: TestResult = early if early is not None else await run_in_threadpool(
        custom.connection_result, connector, config, new_deadline()
    )
    await db.run_sync(credential_health.record_test, body.credential_id, result.ok, result.message)
    return _test_out(result)


def _opened_connection(
    session: Session, connector_id: UUID, credential_id: UUID, user: AdminUser, principal: Principal | None,
) -> tuple[CustomConnector, dict, Optional[TestResult]]:
    org = caller_org(user)
    connector = _connector_or_404(session, connector_id, org)
    row = _scoped_row_or_404(session, principal, credential_id, _EDIT_KEY)
    if row.org_id != org or custom.connector_id_of(row) != connector.id:
        raise HTTPException(status_code=404, detail="no such connection to this connector")
    if row.nodevault:
        return connector, {}, nodevault_untestable_result()
    # Through the recording seam: the whole config is opened, so the read is on the record.
    config = data_access_events.opened_config_for(session, row, consumer="custom_connector_test")
    session.expunge(connector)
    session.commit()
    return connector, config, None
