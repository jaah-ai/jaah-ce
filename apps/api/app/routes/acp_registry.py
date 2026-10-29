"""The ACP registry browser and install — `/api/agent-registry` (D-REG-1, R11; rules: `app/acp_registry.py`).

Any member who views agents browses the registry; only an org admin installs from it, and an install
re-reads the registry server-side, never trusting a client body. An installed agent is an ordinary org
`agents` row (`routes/agents.py` lists, disables and deletes it).

Each registry agent is a provider-grid row, `acp:<id>` with `api_key` (`app/provider_auth_policy.py`). Both routes
answer 403 (`REGISTRY_OFF`'s sentence) while the caller's org has no such row on. The browser lists the entries whose
row is on; an operator-plane org also sees the others, each refused with the sentence naming its row. An install of
an entry whose row is off is a 422 naming it. A registry the control plane cannot read is a 502
(`acp_registry.FETCH_FAILED`); a refused entry a 422 `{"detail", "errors"}` like every agent refusal.
"""
from __future__ import annotations

import re
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import account_auth, acp_registry, acp_registry_icons, provider_auth_policy
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.clock import utcnow as _now
from app.db import get_session
from app.entity_shape import new_uuid7
from app.models.agent_sessions import Agent
from app.models.identity import AdminUser
from app.routes.agents import AgentOut, _committed, _out
from app.tenancy.ids import is_tenant_org

router = APIRouter(tags=["agents"])

#: What the routes call the registry's rows when none is on (`provider_auth_policy.off_sentence`).
REGISTRY_OFF = "ACP registry agents"
ADMIN_ONLY = "Only org admins can install registry agents"


def _registry_on_plan(
    principal: Principal = Depends(current_principal), session: Session = Depends(get_session),
) -> None:
    """403 unless the caller's org's provider grid has a registry row on (`provider_auth_policy.registry_on`)."""
    if principal.org_id is None or not provider_auth_policy.registry_on(session, principal.org_id):
        raise HTTPException(
            status_code=403, detail=provider_auth_policy.off_sentence(principal.org_id, REGISTRY_OFF, plural=True),
        )


_ON_PLAN = [Depends(_registry_on_plan)]
_KEY = account_auth.AUTH_KIND_API_KEY


class RegistryEntryOut(BaseModel):
    id: str
    name: str
    version: str
    description: str
    authors: list[str]
    license: Optional[str]
    website: Optional[str]
    repository: Optional[str]
    icon: Optional[str]
    distribution: Optional[Literal["binary", "npx", "uvx"]]
    key_delivery: Optional[str]
    installable: bool
    refusal: Optional[str]
    installed_agent_id: Optional[UUID]
    installed_version: Optional[str]


def _entries() -> list[dict]:
    try:
        return acp_registry.fetch()
    except acp_registry.RegistryUnavailable:
        raise HTTPException(status_code=502, detail=acp_registry.FETCH_FAILED) from None


def _text(entry: dict, field: str) -> Optional[str]:
    value = entry.get(field)
    return value if isinstance(value, str) and value else None


def normalise_license(value: Optional[str]) -> Optional[str]:
    """The registry's licence as one spelling: "proprietary" in any case is "Proprietary", and an SPDX id written
    with a space before its version ("Apache 2.0") is the id ("Apache-2.0"). Anything else keeps its case."""
    if value is None:
        return None
    text = value.strip()
    if text.lower() == "proprietary":
        return "Proprietary"
    return re.sub(r"(?<=[A-Za-z]) (?=\d)", "-", text)


def _entry_out(
    entry: dict, installed: Optional[tuple[UUID, str]], icon: Optional[str], off: Optional[str] = None,
) -> RegistryEntryOut:
    """One entry; `off` is the sentence refusing its grid row (shown to an operator-plane org only)."""
    distribution: Optional[str] = None
    route: Optional[str] = None
    refusal: Optional[str] = None
    try:
        picked, route = acp_registry.installable(entry)
        distribution = picked["kind"]
    except acp_registry.Refused as exc:
        refusal = exc.sentence
    refusal = off or refusal
    return RegistryEntryOut(
        id=entry["id"], name=entry["name"], version=entry["version"].strip(), description=_text(entry, "description") or "",
        authors=[a for a in entry.get("authors") or [] if isinstance(a, str)], license=normalise_license(_text(entry, "license")),
        website=_text(entry, "website"), repository=_text(entry, "repository"), icon=icon,
        distribution=distribution, key_delivery=route, installable=refusal is None, refusal=refusal,
        installed_agent_id=installed[0] if installed else None, installed_version=installed[1] if installed else None,
    )


@router.get("/api/agent-registry", response_model=list[RegistryEntryOut], dependencies=_ON_PLAN)
@feature("agents", "view")
def list_registry(
    _: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[RegistryEntryOut]:
    """The registry's entries, each with whether it can be installed (or why not) and this org's install.

    `icon` is the entry's icon inlined as a `data:` URI (`acp_registry_icons`), or null: the console's CSP admits
    `data:` images and not the registry's CDN. An entry whose grid row is off is hidden from a tenant and shown refused,
    with the sentence naming the row, to an operator-plane org (which turns rows on in its Providers override)."""
    allowed = provider_auth_policy.allowed(session, principal.org_id)
    operator_plane = not is_tenant_org(principal.org_id)

    def off(entry: dict) -> Optional[str]:
        row = provider_auth_policy.registry_row(entry["id"])
        if (row, _KEY) in allowed:
            return None
        return provider_auth_policy.refusal(session, principal.org_id, row, _KEY, label=entry["name"])

    refused = [(entry, off(entry)) for entry in _entries()]
    shown = [(entry, why) for entry, why in refused if operator_plane or why is None]
    icons = acp_registry_icons.data_uris(_text(entry, "icon") for entry, _ in shown)
    installed = {
        slug: (agent_id, version)
        for agent_id, slug, version in session.execute(
            select(Agent.id, Agent.slug, Agent.version).where(
                Agent.org_id == principal.org_id, Agent.is_catalog.is_(False),
                Agent.manifest["kind"].astext == acp_registry.MANIFEST_KIND,
            )
        )
    }
    return [
        _entry_out(entry, installed.get(entry["id"]), icons.get(_text(entry, "icon") or ""), why)
        for entry, why in shown
    ]


def _refused(sentence: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": sentence, "errors": [sentence]})


@router.post(
    "/api/agent-registry/{registry_id}/install", response_model=AgentOut, status_code=201, dependencies=_ON_PLAN,
)
@feature("agents", "edit")
def install_registry_agent(
    registry_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """Install `registry_id` as this org's agent, pinned at the registry's current version and distribution."""
    if not principal.is_org_admin:
        raise HTTPException(status_code=403, detail=ADMIN_ONLY)
    entry = acp_registry.find(_entries(), registry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No agent {registry_id} in the ACP registry")
    provider_auth_policy.require_allowed(
        session, principal.org_id, provider_auth_policy.registry_row(entry["id"]), _KEY, label=entry["name"],
    )
    try:
        distribution, _ = acp_registry.installable(entry)
    except acp_registry.Refused as exc:
        return _refused(exc.sentence)
    taken = f"{entry['name']} is already installed"
    existing = session.scalars(
        select(Agent).where(Agent.org_id == principal.org_id, Agent.slug == entry["id"]).limit(1)
    ).first()
    if existing is not None:
        detail = taken if acp_registry.is_registry(existing) else f"An agent named {entry['id']} already exists"
        raise HTTPException(status_code=409, detail=detail)
    row = Agent(
        id=new_uuid7(), org_id=principal.org_id, is_catalog=False,
        **acp_registry.row_fields(entry, distribution, installer_id=user.id, now=_now()),
    )
    stamp_actor(row, user, created=True)
    session.add(row)
    if not _committed(session, row):
        raise HTTPException(status_code=409, detail=taken)
    session.refresh(row)
    return JSONResponse(status_code=201, content=_out(row).model_dump(mode="json"))
