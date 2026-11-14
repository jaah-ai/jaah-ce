"""The integration catalog's API: the gallery (`GET /api/integrations`), vendor logos, and one-click Connect.

Every vendor is data (`app/integrations/catalog.py`): these routes serve any spec, so adding a vendor adds no route.
Connect's callback is the EXISTING `GET /api/credentials/connect/callback` (`routes/credentials/connect.py`), the one
redirect URI every vendor app registers; a pending attempt is finished by `finish_pending_connect` below, which
creates the row only once the vendor has granted, so an abandoned attempt leaves nothing behind. A provider with
`ConnectHooks` (the forges) keeps the callback path its vendor app registered before the engine existed; that route
hands the callback to `finish_hooked_callback`, so the move needed no change at any vendor.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Literal, Optional
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import db_async, login_funnel, oauth2_wire, oauth_flow_cookie, oauth_providers
from app.actors import stamp_actor
from app.auth import require_operator, require_user
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import always, feature
from app.authz.object_access import require_project_key, stamp_owner_on_create
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.authz.visibility import scoped_credential_select_for
from app.config import console_url
from app.credentials import health as credential_health
from app.credentials.registry import CREDENTIAL_TYPES
from app.db_async import DbDep
from app.entity_shape import or_404, refused_as
from app.integrations import catalog
from app.integrations import connect as integration_connect
from app.integrations import custom as integration_custom
from app.integrations.connect import ConnectHooks, PendingConnect
from app.models.credentials import Credential, ProjectCredential
from app.models.identity import AdminUser
from app.models.integrations import CustomConnector, IntegrationVote
from app.oauth_providers import OAuthProvider
from app.operator_settings import reads as settings_reads
from app.routes.credentials.connect import _CONNECT_ERRORS, connect_flow
from app.routes.credentials.credential_wire import (
    _audit,
    _connect_redirect_uri,
    _credential_ownership,
    _refuse_duplicates_in_home,
    _scrubbed_write,
    _validated_name,
    in_edition,
)
from app.routes import integrations_custom
from app.routes.public import check_rate_limit
from app.routes.credentials.project_pool import _pool_project_or_404, _require_pool_manager
from app.tenancy.seam import bound_org

router = APIRouter(tags=["integrations"])

# An outcome plus an id or slug, never a code, token, secret or config.
log = logging.getLogger(__name__)

_VIEW_KEY = permission_key("credentials", "view")
_PROJECTS_EDIT_KEY = permission_key("projects", "edit")

# How a connection reads on its card, worst first: the card shows the worst of the caller's rows of that type.
WorstState = Literal["needs_reconnect", "test_failed", "untested", "ok"]

# Immutable: a logo's URL changes only with its bytes' release (the catalog ships in the image).
_LOGO_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    # Belt behind the load-time check (`catalog.check_logo`): opened directly, the SVG still runs nothing.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


class AuthMethodOut(BaseModel):
    kind: str
    primary: bool


class OAuthStateOut(BaseModel):
    """`app_configured`: the deployment's app is designated. `byo_allowed`: a person's own app is accepted.
    `state` is `ready`, or `app_not_configured` while no Jaah app is designated (AC7.2): Connect then needs BYO.
    `hooked`: the vendor's Connect fixes who owns the row (a forge connection is the person's own, a Slack workspace
    the org's) and takes no project, so the screen offers no such choice. `origin_field`: the credential type's url
    field an origin-scoped vendor's Connect asks for first (Backlog's space URL, `OAuthProvider.origin_field`), sent
    as the start's `origin`; None for every other vendor."""

    app_configured: bool
    byo_allowed: bool
    state: Literal["ready", "app_not_configured"]
    hooked: bool = False
    origin_field: Optional[str] = None


class McpOut(BaseModel):
    url: str
    url_editable: bool
    transport: str


class ConnectedOut(BaseModel):
    count: int
    worst_state: WorstState


class IntegrationOut(BaseModel):
    """One gallery card. `credential_type` is the type a connection is saved as (`GET /api/credentials/types`);
    `docs_url` the reader page; `vendor_docs_url` where the vendor documents its auth."""

    slug: str
    label: str
    category: str
    category_label: str
    summary: str
    credential_type: str
    logo_url: Optional[str]
    auth_methods: list[AuthMethodOut]
    oauth: Optional[OAuthStateOut]
    mcp: Optional[McpOut]
    docs_url: str
    vendor_docs_url: str
    connected: Optional[ConnectedOut]
    # Whether the caller has voted for this vendor (`PUT /api/integrations/{slug}/vote`): a Beta card's Vote reads it.
    voted: bool = False
    custom: bool = False
    # A custom connector's card (`custom: true`): the connector its define/detail page edits.
    custom_connector_id: Optional[UUID] = None


class IntegrationConnectStartIn(BaseModel):
    """The one-click Connect's choices: the new row's `name`, its `owner` (the scope chooser: `org` or `personal`;
    a project scope is `owner=org` plus `project_ids`), and optionally a person's own OAuth app (BYO) and the
    optional scopes kept (as `POST /api/credentials/connect/start/{id}` takes them). `name` is required except by a
    hooked provider, which names a new row itself when it is blank. `return_to` and `app` are a hooked provider's
    (`ConnectHooks.prepare` checks them): the page a forge Connect lands back on, and GitHub's app kind.
    `origin` is an origin-scoped vendor's host (`OAuthStateOut.origin_field`), required by it and refused by any other."""

    name: str = Field(default="", max_length=200)
    owner: Literal["org", "personal"] = "personal"
    project_ids: list[int] = Field(default_factory=list, max_length=50)
    client_id: Optional[str] = Field(default=None, max_length=512)
    client_secret: Optional[str] = Field(default=None, max_length=2048)
    scopes: Optional[list[str]] = Field(default=None, max_length=64)
    return_to: Optional[str] = Field(default=None, max_length=200)
    app: Optional[str] = Field(default=None, max_length=64)
    origin: Optional[str] = Field(default=None, max_length=300)


class IntegrationConnectStartOut(BaseModel):
    authorize_url: str


# ── The gallery ───────────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/integrations", response_model=list[IntegrationOut])
# The catalog is served like `GET /api/credentials/types`: every member may browse and bring their own; the
# connected counts come from the caller's own visible rows only.
@feature("credentials", "view", own_credential=True)
async def list_integrations(
    db: DbDep,
    caller: AsyncCaller = Depends(current_caller),
) -> list[IntegrationOut]:
    """Every catalog vendor this edition offers, by label: its methods, its OAuth state, its MCP server and the
    worst state over the caller's visible connections of its type. No plan gate (R8)."""
    specs = [spec for spec in catalog.listed() if in_edition(spec.credential_type)]
    states = await db.run_sync(_connected_states, caller.principal, sorted({spec.credential_type for spec in specs}))
    voted = await db.run_sync(_voted_slugs, caller.actor)
    customs = await db.run_sync(_custom_cards, caller.actor, caller.principal)
    await db_async.release(db)
    # Off the loop: the OAuth state reads operator settings, whose cache miss opens its own session.
    docs_root = await run_in_threadpool(_docs_root)
    oauth = await run_in_threadpool(_oauth_states, specs)
    # A generic card (category `custom`: Generic API key, Generic login, the two "Add custom" cards) adopts a
    # generic type that custom-connector connections are saved as too, so it counts none.
    cards = [
        _integration_out(
            spec, None if spec.category == integration_custom.CATEGORY else states.get(spec.credential_type),
            oauth.get(spec.slug), docs_root, voted=spec.slug in voted,
        )
        for spec in specs
    ]
    cards += [_custom_out(connector, rows, docs_root) for connector, rows in customs]
    return sorted(cards, key=lambda card: card.label.lower())


def _custom_cards(
    session: Session, user: AdminUser, principal: Principal | None
) -> list[tuple[CustomConnector, list[Credential]]]:
    """The caller's org's custom connectors, each with the connections to it the caller may see (R5)."""
    org = integrations_custom.caller_org(user)
    links = integrations_custom.visible_connections(session, principal, org)
    return [(row, links.get(row.id, [])) for row in integrations_custom.org_connectors(session, org)
            if in_edition(integration_custom.credential_type_for(row))]


#: The gallery method a custom connector's scheme reads as.
_CUSTOM_METHOD = {"bearer": "key", "header_key": "key", "basic": "basic", "oauth2_client": "oauth", "none": "mcp"}


def _custom_out(connector: CustomConnector, rows: list[Credential], docs_root: str) -> IntegrationOut:
    """A custom connector as a gallery card: `custom: true`, category `custom`, documented by its generic card."""
    page = "custom_mcp" if connector.kind == "mcp" else "custom_connector"
    connected: Optional[ConnectedOut] = None
    if rows:
        worst = (
            "needs_reconnect" if any(row.needs_reconnect_at is not None for row in rows)
            else "test_failed" if any(row.last_test_ok is False for row in rows)
            else "untested" if any(row.last_tested_at is None for row in rows) else "ok"
        )
        connected = ConnectedOut(count=len(rows), worst_state=worst)
    return IntegrationOut(
        slug=connector.slug, label=connector.name, category=integration_custom.CATEGORY,
        category_label=catalog.CATEGORIES[integration_custom.CATEGORY],
        summary=connector.description or connector.base_url,
        credential_type=integration_custom.credential_type_for(connector), logo_url=None,
        auth_methods=[AuthMethodOut(kind="mcp" if connector.kind == "mcp" else _CUSTOM_METHOD[connector.auth_scheme],
                                    primary=True)],
        oauth=None,
        mcp=McpOut(url=connector.base_url, url_editable=False, transport=connector.mcp_transport or "streamable_http")
        if connector.kind == "mcp" else None,
        docs_url=_reader_page(docs_root, integration_custom.CATEGORY, page),
        vendor_docs_url="", connected=connected, custom=True, custom_connector_id=connector.id,
    )


def _reader_page(docs_root: str, category: str, slug: str) -> str:
    """A vendor's generated reader page: kebab-case category and slug, as the integration docs generator names them."""
    if not docs_root:
        return ""
    return f"{docs_root}/using-jaah/integrations/{category.replace('_', '-')}/{slug.replace('_', '-')}"


def _docs_root() -> str:
    return settings_reads.get_str("signup_docs_url").rstrip("/")


def _oauth_states(specs: list[catalog.IntegrationSpec]) -> dict[str, OAuthStateOut]:
    out: dict[str, OAuthStateOut] = {}
    for spec in specs:
        provider = oauth_providers.PROVIDERS.get(spec.slug) if spec.provider is not None else None
        if provider is None:
            continue
        configured = integration_connect.app_configured(provider)
        out[spec.slug] = OAuthStateOut(
            app_configured=configured, byo_allowed=integration_connect.byo_allowed(provider),
            state="ready" if configured else "app_not_configured",
            hooked=integration_connect.hooks_for(provider) is not None, origin_field=provider.origin_field,
        )
    return out


def _connected_states(session: Session, principal: Principal | None, types: list[str]) -> dict[str, ConnectedOut]:
    """`{type: (count, worst state)}` over the rows the caller may see, in one grouped query."""
    if not types:
        return {}
    visible = scoped_credential_select_for(principal, _VIEW_KEY).where(Credential.type.in_(types)).subquery()
    rows = session.execute(
        select(
            visible.c.type,
            func.count(),
            func.count().filter(visible.c.needs_reconnect_at.is_not(None)),
            func.count().filter(visible.c.last_test_ok.is_(False)),
            func.count().filter(visible.c.last_tested_at.is_(None)),
        ).group_by(visible.c.type)
    ).all()
    out: dict[str, ConnectedOut] = {}
    for type_slug, count, dead, failed, untested in rows:
        worst = "needs_reconnect" if dead else "test_failed" if failed else "untested" if untested else "ok"
        out[type_slug] = ConnectedOut(count=count, worst_state=worst)
    return out


def _integration_out(
    spec: catalog.IntegrationSpec, connected: Optional[ConnectedOut], oauth: Optional[OAuthStateOut], docs_root: str,
    *, voted: bool = False,
) -> IntegrationOut:
    return IntegrationOut(
        slug=spec.slug, label=spec.label, category=spec.category, category_label=catalog.CATEGORIES[spec.category],
        summary=spec.summary, credential_type=spec.credential_type,
        logo_url=f"/api/integrations/logos/{spec.slug}.svg" if spec.logo_svg else None,
        auth_methods=[AuthMethodOut(kind=method.kind, primary=index == 0) for index, method in enumerate(spec.auth)],
        oauth=oauth,
        mcp=McpOut(url=spec.mcp.url, url_editable=spec.mcp.url_editable, transport=spec.mcp.transport)
        if spec.mcp is not None else None,
        docs_url=_reader_page(docs_root, spec.category, spec.slug),
        vendor_docs_url=spec.vendor_docs_url,
        connected=connected,
        voted=voted,
    )


# ── Votes for a Beta vendor ───────────────────────────────────────────────────────────────────────────────


class IntegrationVoteOut(BaseModel):
    voted: bool


def _voted_slugs(session: Session, user: AdminUser) -> set[str]:
    """The vendors the caller's person has voted for; none for a seat with no identity."""
    if user.identity_id is None:
        return set()
    return set(session.scalars(select(IntegrationVote.slug).where(IntegrationVote.identity_id == user.identity_id)))


def _votable_or_404(slug: str) -> catalog.IntegrationSpec:
    """A vendor the gallery lists in this edition with an OAuth method: the only cards that can be Beta."""
    spec = catalog.spec_for(slug)
    listed = spec is not None and any(item.slug == slug for item in catalog.listed())
    if spec is None or not listed or spec.provider is None or not in_edition(spec.credential_type):
        raise HTTPException(status_code=404, detail="no such integration")
    return spec


@router.put("/api/integrations/{slug}/vote", response_model=IntegrationVoteOut)
# Whoever may browse the gallery may vote on it, as on `GET /api/integrations`.
@feature("credentials", "view", own_credential=True)
async def vote_integration(slug: str, db: DbDep, user: AdminUser = Depends(require_user)) -> IntegrationVoteOut:
    """The caller's person votes for `slug`: one vote per person, so a repeat (another org, a double click) is the same
    vote. 409 for a seat with no identity (a machine user has no person to vote) and for a vendor no longer Beta."""
    spec = _votable_or_404(slug)
    if user.identity_id is None:
        raise HTTPException(status_code=409, detail="Only a person can vote.")
    # Only a Beta vendor takes a new vote (withdrawing stays open, so a vote cast before its app was set can go).
    states = await run_in_threadpool(_oauth_states, [spec])
    if slug not in states or states[slug].state != "app_not_configured":
        raise HTTPException(status_code=409, detail="This integration is ready: connect it instead.")
    identity_id = user.identity_id

    def write(session: Session) -> None:
        session.execute(
            pg_insert(IntegrationVote).values(identity_id=identity_id, slug=slug).on_conflict_do_nothing()
        )
        session.commit()

    await db.run_sync(write)
    return IntegrationVoteOut(voted=True)


@router.delete("/api/integrations/{slug}/vote", response_model=IntegrationVoteOut)
@feature("credentials", "view", own_credential=True)
@db_async.on_session
def unvote_integration(
    session: Session, slug: str, user: AdminUser = Depends(require_user),
) -> IntegrationVoteOut:
    """The caller's person withdraws their vote for `slug`; withdrawing none is not an error."""
    _votable_or_404(slug)
    identity_id = user.identity_id
    if identity_id is not None:
        session.execute(
            delete(IntegrationVote).where(IntegrationVote.identity_id == identity_id, IntegrationVote.slug == slug)
        )
        session.commit()
    return IntegrationVoteOut(voted=False)


class IntegrationAppOut(BaseModel):
    """One vendor on the operator's Settings > Integrations > OAuth apps list. `app_knob`: the `oauth_app.<slug>`
    knob that designates Jaah's app (`PUT /api/settings`), null for a vendor whose app is set elsewhere or never.
    `beta`: no Jaah app, so its card offers Vote. `votes` and `connections` count every org."""

    slug: str
    label: str
    logo_url: Optional[str]
    app_knob: Optional[str]
    beta: bool
    votes: int
    connections: int


@router.get("/api/integrations/apps", response_model=list[IntegrationAppOut])
@feature("system", "view")
async def list_integration_apps(db: DbDep, _: AdminUser = Depends(require_operator)) -> list[IntegrationAppOut]:
    """Every OAuth vendor this edition lists, most voted first: its Jaah app state, its votes and its connections
    across every org."""
    specs = [spec for spec in catalog.listed() if spec.provider is not None and in_edition(spec.credential_type)]
    await db_async.release(db)
    oauth = await run_in_threadpool(_oauth_states, specs)
    votes, connections = await _fleet_counts(sorted({spec.credential_type for spec in specs}))
    rows = [
        IntegrationAppOut(
            slug=spec.slug, label=spec.label,
            logo_url=f"/api/integrations/logos/{spec.slug}.svg" if spec.logo_svg else None,
            app_knob=spec.app_knob, beta=spec.slug in oauth and oauth[spec.slug].state == "app_not_configured",
            votes=votes.get(spec.slug, 0), connections=connections.get(spec.credential_type, 0),
        )
        for spec in specs
    ]
    return sorted(rows, key=lambda row: (-row.votes, row.label.lower()))


async def _fleet_counts(types: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """`({slug: votes}, {credential type: connections})` across every org, read on the operator plane on a session of
    the function's own (connections are org-scoped rows; votes are `Shared`). Counts only, never a row."""
    from app.tenancy import seam

    def read(fleet: Session) -> tuple[dict[str, int], dict[str, int]]:
        votes = dict(fleet.execute(
            select(IntegrationVote.slug, func.count()).group_by(IntegrationVote.slug)
        ).tuples().all())
        connections = dict(fleet.execute(
            select(Credential.type, func.count()).where(Credential.type.in_(types)).group_by(Credential.type)
        ).tuples().all()) if types else {}
        return votes, connections

    with seam.detached(), seam.operator_scope():
        return await db_async.run_in_session(read)


@router.get("/api/integrations/logos/{slug}.svg")
@feature("credentials", "view", own_credential=True)
async def get_integration_logo(slug: str, user: AdminUser = Depends(require_user)) -> Response:
    """A vendor's logo, checked at load (no script, no external reference) and served immutable."""
    spec = or_404(catalog.spec_for(slug), detail="no such logo")
    return Response(content=or_404(spec.logo_svg, detail="no such logo"), media_type="image/svg+xml",
                    headers=_LOGO_HEADERS)


@router.get("/api/public/integrations/{slug}/client-metadata.json", dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
async def get_client_metadata(slug: str) -> Response:
    """The Client ID Metadata Document of a `client_metadata` vendor (PostHog): the vendor fetches it to learn who
    Jaah is, so it is public, and its `client_id` is this document's own URL (`oauth_providers.client_metadata_url`).
    It names only public facts: the name, the redirect URI and the scopes Connect asks for."""
    provider = oauth_providers.PROVIDERS.get(slug)
    spec = catalog.spec_for(slug)
    if provider is None or spec is None or not provider.client_metadata or not in_edition(spec.credential_type):
        raise HTTPException(status_code=404, detail="no such client")
    document = {
        "client_id": oauth_providers.client_metadata_url(provider),
        "client_name": "Jaah",
        "client_uri": console_url("/"),
        "redirect_uris": [_connect_redirect_uri()],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": oauth_providers.requested_scope(provider) or "",
    }
    # Short-lived cache: a vendor re-reads it, and a changed redirect URI must reach it the same day.
    return JSONResponse(document, headers={"Cache-Control": "public, max-age=300"})


# ── One-click Connect ─────────────────────────────────────────────────────────────────────────────────────


@router.post("/api/integrations/{slug}/connect/start", response_model=IntegrationConnectStartOut)
# Creating a credential: `POST /api/credentials`'s declaration, since the callback creates the row.
@feature("credentials", "create", own_credential=True)
async def start_integration_connect(
    slug: str,
    request: Request,
    response: Response,
    db: DbDep,
    body: Optional[IntegrationConnectStartIn] = Body(default=None),
    caller: AsyncCaller = Depends(current_caller),
) -> IntegrationConnectStartOut:
    """Begin a Connect for `slug` with no row saved: the choices are checked now (so no one consents only to be
    refused), sealed into the flow cookie, and the row is created by the shared callback. 404 for a vendor with no
    OAuth method (and an unlisted provider without an `on_grant` hook); 409 `app_not_configured` while Jaah's app is
    not designated and no BYO client was given. A hooked provider's own refusals come from its `ConnectHooks`."""
    body = body or IntegrationConnectStartIn()
    provider = integration_connect.provider_for(slug)
    spec = catalog.spec_for(slug)
    if provider is None or (spec is not None and not in_edition(spec.credential_type)):
        raise HTTPException(status_code=404, detail=f"no OAuth connect for {slug[:64]!r}")
    if spec is not None and spec.provider is not None and not provider.scopes and spec.provider.scopes:
        # A spec layered over a hand-written provider that pins no scope catalogue (per-API scopes, so no default)
        # brings its own for this Connect only; the registered provider and its row-bound Connect are untouched.
        provider = replace(provider, scopes=spec.provider.scopes, scope_separator=spec.provider.scope_separator)
    hooks = integration_connect.hooks_for(provider)
    if hooks is None and (body.return_to or body.app):
        raise HTTPException(status_code=400, detail=f"{provider.label or slug[:64]} takes no return_to or app.")
    name = _validated_name(body.name) if hooks is None or body.name.strip() else ""
    origin = _validated_origin(provider, spec, body.origin)
    ownership = _credential_ownership(body.owner, caller.principal)
    project_ids = tuple(dict.fromkeys(body.project_ids))
    pending = PendingConnect(
        integration=slug, name=name, owner="org" if ownership == "org" else "personal", user_id=caller.actor.id,
        project_ids=project_ids, client_id=(body.client_id or "").strip(), client_secret=body.client_secret or "",
        return_to=body.return_to or "", app=body.app or "", origin=origin,
    )
    if hooks is not None:
        # The hook checks and completes the choices (its operator settings are read off the loop).
        pending = await run_in_threadpool(hooks.prepare, pending, caller.actor)
    elif spec is not None:
        await db.run_sync(_check_new_row, caller.principal, pending, spec.credential_type)
    else:
        await db.run_sync(_check_projects, caller.principal, pending, _transient_row(caller.principal, pending, "generic_api_key"))
    await db_async.release(db)
    try:
        client = await run_in_threadpool(_attempt_client, provider, hooks, pending)
    except integration_connect.ClientRefusal as refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail) from None
    with refused_as(400, oauth_providers.ScopeChoiceError):
        scope = oauth_providers.requested_scope(provider, chosen=body.scopes)

    pkce = oauth2_wire.new_pkce_pair()
    state = oauth2_wire.new_state()
    url = oauth_providers.authorize_url(
        provider, client_id=client.client_id, redirect_uri=_redirect_uri(provider, hooks), state=state,
        challenge=pkce.challenge if provider.pkce else "", scope=scope,
        endpoint=client.authorize_endpoint, extra=client.authorize_extra,
    )
    sealed = integration_connect.seal_pending(
        replace(pending, scope=(scope or "") if provider.scopes else ""), state=state
    )
    flow_cookie = connect_flow(hooks.cookie_path, slug) if hooks is not None else connect_flow()
    flow_cookie.set(response, {
        "state": state, "verifier": pkce.verifier, "user_id": caller.actor.id, "credential_id": "", "scope": "",
        "integration": slug, "pending": sealed,
    })
    # As the row-bound start does: an older `SameSite=Strict` session cookie would be withheld on the callback.
    login_funnel.reissue_session(request, response, caller.actor)
    log.info("integration connect: user=%s started an attempt on %s", caller.actor.id, slug)
    return IntegrationConnectStartOut(authorize_url=url)


def _validated_origin(provider: OAuthProvider, spec: Optional[catalog.IntegrationSpec], value: Optional[str]) -> str:
    """The attempt's host for an origin-scoped provider (`oauth_providers.scoped_origin`), else "": 400 when such a
    provider gets none it accepts, or another provider gets one."""
    if provider.origin_field is None:
        if value and value.strip():
            raise HTTPException(status_code=400, detail=f"{provider.label or 'This vendor'} takes no origin.")
        return ""
    origin = oauth_providers.scoped_origin(provider, value)
    if origin is None:
        label = provider.origin_field
        if spec is not None:
            label = next((item.label for item in CREDENTIAL_TYPES[spec.credential_type].fields
                          if item.key == provider.origin_field), label)
        hosts = ", ".join(provider.origin_hosts)
        # Mid-sentence, the label is lowercased the way the testers' sentences do ("space URL").
        named = f"{label[:1].lower()}{label[1:]}"
        if provider.origin_label:
            raise HTTPException(status_code=400, detail=f"Enter your {named}: acme for acme.{provider.origin_hosts[0]}.")
        raise HTTPException(status_code=400, detail=f"Enter your {named}: an address ending in {hosts}.")
    return origin


def _redirect_uri(provider: OAuthProvider, hooks: Optional[ConnectHooks]) -> str:
    """The callback the vendor's app registered: the shared one, or a hooked provider's own."""
    if hooks is None:
        return _connect_redirect_uri()
    return console_url(hooks.redirect_path(provider))


def _attempt_client(
    provider: OAuthProvider, hooks: Optional[ConnectHooks], pending: PendingConnect
) -> integration_connect.ConnectClient:
    """The attempt's OAuth client: a hooked provider's own resolution, else the person's (BYO) or the deployment's
    app (`client_for`). Blocking (operator settings, the secret's opening); raises `ClientRefusal`."""
    if hooks is not None:
        return hooks.client(provider, pending)
    client_id, client_secret = integration_connect.client_for(provider, pending, consumer="integration_connect")
    if provider.origin_field is None:
        return integration_connect.ConnectClient(client_id=client_id, client_secret=client_secret)
    # An origin-scoped provider's endpoints, filled from the attempt's checked host (`_validated_origin`).
    config = integration_connect.pending_config(provider, pending)
    return integration_connect.ConnectClient(
        client_id=client_id, client_secret=client_secret,
        authorize_endpoint=oauth_providers.endpoint_for(provider, provider.authorize_url, config),
        token_endpoint=oauth_providers.endpoint_for(provider, provider.token_url, config),
    )


def _row_org(principal: Principal | None) -> Optional[UUID]:
    """The org a connected row is born in: the request's bound org (a tenant, or an operator acting as one), else
    the connecting person's own (the operator plane stamps nothing, so the create names it)."""
    return bound_org() or (principal.org_id if principal is not None else None)


def _transient_row(principal: Principal | None, pending: PendingConnect, type_slug: str) -> Credential:
    """The row the attempt will create, unsaved: its ownership decided as `POST /api/credentials` decides it."""
    row = Credential(name=pending.name, type=type_slug, description="", org_id=_row_org(principal))
    stamp_owner_on_create(principal, row, pending.owner)
    return row


def _check_new_row(session: Session, principal: Principal | None, pending: PendingConnect, type_slug: str) -> None:
    """The create's own refusals, asked before consent and again at the callback: a name taken in its home, and a
    project the caller may not attach the row to. Never adds the transient row to the session."""
    row = _transient_row(principal, pending, type_slug)
    _refuse_duplicates_in_home(session, [(row, pending.name, None)])
    _check_projects(session, principal, pending, row)


def _check_projects(session: Session, principal: Principal | None, pending: PendingConnect, row: Credential) -> None:
    """403/404 for a project the caller may not attach `row` to (`POST /api/projects/{id}/credentials`'s checks)."""
    for project_id in pending.project_ids:
        _pool_project_or_404(session, principal, project_id)
        require_project_key(principal, _PROJECTS_EDIT_KEY, project_id,
                            detail=f"{_PROJECTS_EDIT_KEY} on the project is required to attach it")
        _require_pool_manager(principal, row, attaching=True)


# ── The callback's pending arm (called by `routes/credentials/connect.py::credential_connect_callback`) ──────


def new_page(slug: str, error: str) -> RedirectResponse:
    """Back to the vendor's Connect screen with one slug of the flow's closed vocabulary."""
    if error not in _CONNECT_ERRORS:
        raise ValueError(f"{error!r} is not a member of the closed credential-connect error vocabulary")
    return RedirectResponse(
        console_url(f"/credentials/new?{urlencode(integration_connect.redirect_query(error, slug))}"), status_code=303
    )


async def finish_pending_connect(
    db: Any, flow: dict[str, str], *, code: str, error: str, user: AdminUser, principal: Principal | None,
    error_description: str = "",
) -> RedirectResponse:
    """Spend the code of a pending attempt, create its row (or run the provider's `on_grant` hook), run the Test and
    send the browser to `/credentials/<id>?connected=1` (a hooked provider: its own `finish` page). Every refusal is a
    redirect carrying one closed slug. `error_description` is the vendor's text beside `error`: read by a hooked
    provider's `refusal` only, never echoed or logged."""
    slug = flow.get("integration") or ""
    pending = integration_connect.open_pending(flow.get("pending") or "", state=flow["state"])
    provider = integration_connect.provider_for(slug)
    hooks = integration_connect.hooks_for(provider) if provider is not None else None

    def refused(refusal: str) -> RedirectResponse:
        return hooks.finish(slug, pending, refusal) if hooks is not None else new_page(slug, refusal)

    if pending is None or pending.integration != slug or pending.user_id != user.id:
        pending = None
        return refused("expired")
    if provider is None:
        return refused("bad_request")
    if error:
        log.info("integration connect: user=%s did not complete authorization at %s", user.id, slug)
        return refused(hooks.refusal(provider, "denied", f"{error} {error_description}") if hooks is not None else "denied")
    if not code:
        return refused("no_code")

    refusal, grant, account, client, learned = await run_in_threadpool(_exchange, provider, hooks, pending, code, flow)
    if refusal is not None:
        return refused(refusal)
    assert grant is not None and client is not None  # narrowing: `_exchange` returns a refusal or a grant
    pending = replace(pending, account=account or "")
    if provider.on_grant:
        # A hook may call out (an account read, a token given back): on a worker thread with its own session.
        await db_async.release(db)
        created = await db_async.run_blocking_in_session(
            _create_connected_row, user, principal, provider, grant, pending, client, actor=user
        )
    else:
        created = await db.run_sync(_create_connected_row, user, principal, provider, grant, pending, None, learned)
    if isinstance(created, str):
        return refused(created)
    credential_id = created
    await db_async.release(db)
    log.info("integration connect: credential=%s connected to %s by user=%s", credential_id, slug, user.id)
    if hooks is not None:
        # The connect is committed: what follows (the hook's own reads of the vendor) may only change the page.
        onward = await db_async.run_blocking_in_session(
            _after_grant, hooks, user, provider, grant, pending, credential_id, actor=user
        )
        return onward or hooks.finish(slug, pending, None)
    result = await run_in_threadpool(integration_connect.connected_row_test_result, credential_id)
    if result is not None:
        await db.run_sync(credential_health.record_test, credential_id, result.ok, result.message)
    return RedirectResponse(console_url(f"/credentials/{credential_id}?connected=1"), status_code=303)


def _after_grant(
    session: Session, hooks: ConnectHooks, user: AdminUser, provider: OAuthProvider, grant: oauth2_wire.TokenGrant,
    pending: PendingConnect, credential_id: UUID,
) -> Optional[RedirectResponse]:
    """The hook's post-commit step, which may never fail a connect already stored: any fault is logged by class and
    the browser goes to the success page."""
    try:
        return hooks.after_grant(session, user, provider, grant, pending, credential_id)
    except Exception as exc:  # noqa: BLE001 - the row is committed; nothing here may fail the connect
        log.warning("integration connect: credential=%s post-connect step failed (%s)", credential_id,
                    type(exc).__name__)
        return None


class HookedCallbackQuery:
    """The query a hooked provider's callback route takes (`Depends(HookedCallbackQuery)`, then
    `**vars(query)` into `finish_hooked_callback`): unconstrained defaulted `str`s, as a pydantic 422 would echo
    the rejected input, a code. `error_description` is the vendor's reason text beside `error`; only matched
    (`ConnectHooks.refusal`), never echoed or logged."""

    def __init__(
        self,
        code: str = Query(default=""),
        state: str = Query(default=""),
        error: str = Query(default=""),
        error_description: str = Query(default=""),
    ) -> None:
        self.code, self.state, self.error, self.error_description = code, state, error, error_description


async def finish_hooked_callback(
    request: Request, db: Any, *, slug: str, code: str, state: str, error: str, error_description: str,
    user: AdminUser, principal: Principal | None,
) -> RedirectResponse:
    """A hooked provider's callback (`ConnectHooks.redirect_path`), served by the route at that path: the flow
    cookie's checks (`oauth_flow_cookie.verified_attempt`), then `finish_pending_connect`. The cookie must name a
    pending attempt for `slug` itself, so an attempt never finishes on another provider's path. Every exit retires
    the cookie."""
    provider = oauth_providers.PROVIDERS[slug]
    hooks = integration_connect.hooks_for(provider)
    if hooks is None:  # pragma: no cover - only a hooked provider's route calls this
        raise ValueError(f"{slug}: has no ConnectHooks")
    cookie = connect_flow(hooks.cookie_path, slug)
    flow, refusal = oauth_flow_cookie.verified_attempt(
        cookie, request, callback_state=state, user_id=user.id, log=log, flow_name=f"integration connect:{slug}",
    )
    if refusal is not None:
        response = hooks.finish(slug, _unverified_pending(cookie, request), refusal)
    elif flow is None or flow.get("integration") != slug or not flow.get("pending") or flow.get("credential_id"):
        log.warning("integration connect: user=%s reached the %s callback with another attempt's cookie", user.id, slug)
        response = hooks.finish(slug, None, "expired")
    else:
        try:
            response = await finish_pending_connect(
                db, flow, code=code, error=error, user=user, principal=principal, error_description=error_description,
            )
        except HTTPException as exc:
            # A write the store refused (`_scrubbed_write`'s 500, carrying nothing from the row): a page that says
            # the connection was not stored, never raw JSON on the callback URL.
            log.warning("integration connect: user=%s %s connect could not be stored (%s)", user.id, slug,
                        exc.status_code)
            response = hooks.finish(slug, None, "credential_gone")
    cookie.clear(response)
    return response


def _unverified_pending(cookie: Any, request: Request) -> Optional[PendingConnect]:
    """A refused attempt's pending choice, read from its still-signed cookie only to pick the landing page among the
    hook's allow-listed ones (never trusted for anything else). None when there is none to read."""
    attempt = cookie.read(request)
    if attempt is None:
        return None
    return integration_connect.open_pending(attempt.get("pending") or "", state=attempt["state"])


def _exchange(
    provider: OAuthProvider, hooks: Optional[ConnectHooks], pending: PendingConnect, code: str, flow: dict[str, str]
) -> tuple[
    Optional[str], Optional[oauth2_wire.TokenGrant], Optional[str], Optional[integration_connect.ConnectClient],
    dict[str, str],
]:
    """`(refusal, None, None, None, {})` or `(None, grant, account, client, learned)`, `client` the one that spent
    the code and `learned` what the provider's `on_grant_config` hook read (`learned_from_grant`). Blocking (the
    operator app, the vendor)."""
    try:
        client = _attempt_client(provider, hooks, pending)
    except integration_connect.ClientRefusal as refusal:
        return refusal.slug, None, None, None, {}
    try:
        if hooks is not None and hooks.exchange is not None:
            grant = hooks.exchange(provider, pending, client, code, _redirect_uri(provider, hooks))
        else:
            grant = oauth_providers.exchange_code(
                provider, client_id=client.client_id, client_secret=client.client_secret, code=code,
                redirect_uri=_redirect_uri(provider, hooks), verifier=flow["verifier"] if provider.pkce else "",
                endpoint=client.token_endpoint, accept=client.accept,
            )
    except oauth2_wire.OAuth2Error as exc:
        # `exc.code` only; the vendor's text reaches the hook's `refusal` match and nothing else.
        log.warning("integration connect: %s refused the exchange (code=%s)", provider.key, exc.code)
        slug = hooks.refusal(provider, "exchange_failed", str(exc)) if hooks is not None else "exchange_failed"
        return slug, None, None, None, {}
    if not provider.on_grant and not integration_connect.grant_is_storable(grant, provider):
        log.warning("integration connect: %s returned an expiring token with no refresh token", provider.key)
        return "no_refresh_token", None, None, None, {}
    try:
        learned = integration_connect.learned_from_grant(
            provider, grant, integration_connect.pending_config(provider, pending))
    except integration_connect.GrantRefused as refusal:
        log.warning("integration connect: %s granted nothing a row could reach (%s)", provider.key, refusal.slug)
        return refusal.slug, None, None, None, {}
    account = oauth_providers.account_of(provider, grant.access_token, integration_connect.pending_config(provider, pending))
    return None, grant, account, client, learned


def _create_connected_row(
    session: Session, user: AdminUser, principal: Principal | None, provider: OAuthProvider,
    grant: oauth2_wire.TokenGrant, pending: PendingConnect, client: Optional[integration_connect.ConnectClient] = None,
    learned: Optional[dict[str, str]] = None,
) -> UUID | str:
    """The row's id, or a refusal slug. `client` is the one that spent the code, handed to a hook that must give the
    grant back with it. The provider's `on_grant` hook writes the row when it names one (forges,
    Slack, Gmail); otherwise a catalog row of the vendor's type: tokens, ownership, the projects it attaches to."""
    if provider.on_grant:
        # The engine's own checks run before any hook: the projects the row is attached to (a hook that creates an
        # org row checks its own permission, as its pre-engine route did), then the write, audited and scrubbed.
        try:
            _check_projects(session, principal, pending, _transient_row(principal, pending, "generic_api_key"))
        except HTTPException as exc:
            log.warning("integration connect: user=%s may not attach these projects (%s)", user.id, exc.status_code)
            return "credential_gone"
        with _scrubbed_write(session, "connect"):
            try:
                outcome = integration_connect.call_on_grant(
                    provider.on_grant, session, provider, grant, pending,
                    context=integration_connect.GrantContext(user=user, principal=principal, client=client),
                )
            except integration_connect.GrantRefused as refusal:
                session.rollback()
                return refusal.slug
            row = outcome.credential
            if not provider.connect_hooks:
                # A hooked provider records its own connect (the forges' `git_connected`), as its route did.
                _audit(session, user, "credential_connected", f"Connected {provider.label} credential {row.name}", row,
                       owner_user_id=row.owner_id)
            session.commit()
        if outcome.after_commit is not None:
            outcome.after_commit()
        return row.id
    spec = catalog.spec_for(pending.integration)
    if spec is None:
        return "bad_request"
    try:
        _check_new_row(session, principal, pending, spec.credential_type)
    except HTTPException as exc:
        log.warning("integration connect: user=%s may no longer create this row (%s)", user.id, exc.status_code)
        return "name_taken" if exc.status_code == 409 else "credential_gone"
    type_fields = frozenset(item.key for item in CREDENTIAL_TYPES[spec.credential_type].fields)
    row = Credential(
        name=pending.name, type=spec.credential_type, description="", org_id=_row_org(principal),
        config=integration_connect.grant_config(type_fields, grant, pending, provider, learned),
    )
    stamp_actor(row, user, created=True)
    stamp_owner_on_create(principal, row, pending.owner)
    _refuse_duplicates_in_home(session, [(row, pending.name, None)])
    with _scrubbed_write(session, "connect"):
        session.add(row)
        session.flush()
        for project_id in pending.project_ids:
            session.add(ProjectCredential(project_id=project_id, credential_id=row.id, created_by_id=user.id))
        _audit(session, user, "credential_connected", f"Connected {spec.label} credential {row.name}", row,
               owner_user_id=row.owner_id)
        session.commit()
    return row.id
