"""Credential-account roster CRUD over `accounts`, the SSOT for the accounts runners auth with.

Identity and `enabled` are owned here; quota is sensed via `app/account_fold.py`, the values the
dispatch gate uses. No `config_dir`: it is per node (`app/node_clone.py::resolve_auth_dir`, fails
closed to `""`). DELETE drops the row only; `POST .../deprovision` deletes the host dir first. Known
gap: a token on a mounted `{jaah-home}/auth/.shared` survives deprovision. `accounts.shared` is no
console concept: neither served nor settable here.

Two auth kinds (`app/account_auth.py`): OAuth materializes a per-node dir; an API-key account points
(`credential_id`) at a `credentials` row, created with it in one transaction. The key never reaches
`accounts`, a response or a log (`app/routes/credentials/`'s properties; hence `Any`-typed key
fields). Routes are keyed `accounts:<action>` (`app/authz/`); writes also need `require_account_writer`."""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any, Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import (
    account_auth, account_fold, account_transfer, agent_providers, data_access_events, edition, http_bounded,
    ce_caps, provider_auth_policy, tenant_defaults,
)
from app.credential_crypto import has_sealed_secret, open_secret
from app.account_slug import new_account_slug
# Aliased only because `_account_out` takes the pre-resolved map under the same name.
from app.actors import actor_names as resolve_actor_names, actor_names_for, stamp_actor
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.object_access import OWNERSHIP_PERSONAL, can_access, stamp_owner_on_create, validated_ownership
from app.authz.principal import org_admin_writer, Principal, creates_org_rows, current_principal
from app.authz.visibility import ObjectAccess, readable_account_clause, readable_account_select
from app.credentials.registry import CREDENTIAL_TYPES
# The header-safety rule the Credentials testers apply, imported rather than restated.
from app.credentials.testers import _is_transmittable
from app.db import get_session
from app.entity_shape import EntityOut, StrictModel, constraint_name, entity_fields, get_or_404, iso_or_none
from app.events import emit_console_event
from app.models.credentials import Account, AccountObservation, Credential
from app.models.identity import AdminUser
from app.models.nodes import Node
from starlette.concurrency import run_in_threadpool

from app.routes.commands import (
    _SLUG_RE, SLUG_RULE, _credential_safe_args, _credential_safe_result,
    _record_audit, account_slug_not_available, default_dispatch_node, dispatch_and_audit,
)
from app.tenancy.copy import node_home_unrecorded
from app.tenancy.ids import is_tenant_org
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
# Imported, never restated: `app/routes/credentials/` owns value checks and log-safe writes.
# `_validated_value` rejects a bad key naming the key, never the value; `_scrubbed_write` turns a DB
# failure on a config-bearing statement into a bare 500, defence in depth behind `app/db.py`'s
# engine-wide `hide_parameters=True` (this INSERT binds the API key in its JSONB parameter).
from app.routes.credentials.credential_wire import _scrubbed_write, _validated_value
from app.node_clone import resolve_auth_dir
from app.org_gate import require_org_active
# `current_scope()` only: the bound org, read on the create door where no row exists yet.
from app.tenancy import seam
from app.node_identity import names_by_id, node_id_or_404, node_matches

log = logging.getLogger("app.routes.accounts")

router = APIRouter(tags=["accounts"])


#: The administrator bar on every roster write — `authz.org_admin_writer`.
require_account_writer = org_admin_writer("Account records")


def _slug_collision(
    session: Session, exc: IntegrityError, slug: object, principal: Principal | None, caller_org: object,
) -> HTTPException:
    """The 409 for a `uq_accounts_slug` collision, worded for what the caller is entitled to know.

    `accounts.slug` is fleet-unique, so a tenant's write can collide with a row they cannot see; "already
    exists" would be an existence oracle. So a collision with a row this caller's roster shows says
    "already exists", and any other says "not available", as
    `commands.py::_refuse_slug_held_by_another_org` does on the bus.

    "Shows" is `readable_account_select` (org seam and grant conjunct), with the caller's org named so it
    holds under `SeamMode.SHADOW`. Only a slug collision when Postgres says so
    (`ck_accounts_api_key_has_credential` raises the same type). Called after `session.rollback()`."""
    constraint = constraint_name(exc)
    if constraint != "uq_accounts_slug" or not isinstance(slug, str) or not slug:
        return HTTPException(status_code=409, detail="account slug already exists")
    shown = session.scalar(
        readable_account_select(principal).where(Account.slug == slug, Account.org_id == caller_org)
    ) is not None
    if shown:
        return HTTPException(status_code=409, detail=f"account slug '{slug}' already exists")
    return HTTPException(status_code=409, detail=account_slug_not_available(slug))


class AccountOut(EntityOut):
    slug: str
    # Which vendor's CLI this credential logs into. Accepted on create only: the account materializes under
    # a vendor-specific login, so relabelling would aim readers at a login never performed. NOT NULL,
    # default `agent_providers.DEFAULT_PROVIDER`.
    provider: str
    enabled: bool
    # How this account authenticates (`oauth` or `api_key`, `app/account_auth.py`). Served on every row so
    # the console never infers the kind from other fields. Existing rows answer `oauth`.
    auth_kind: str = account_auth.AUTH_KIND_OAUTH
    # The pointer to the key's `credentials` row, as a string, never anything behind it. `None` for OAuth.
    credential_id: Optional[str] = None
    # The non-secret "a key is stored" indicator: a secret is omitted, never masked
    # (`app/routes/credentials/` property 1), so a boolean is the only honest shape.
    #
    # Derived from the pointer, not the config, so the roster never loads the key.
    # `ck_accounts_api_key_has_credential` and `create_account`'s blank-key refusal make it faithful.
    api_key_set: bool = False
    # Whether the scheduler may pick this account for a task that did not ask for it. Existing rows answer
    # true (the column's server default).
    auto_assign: bool = True
    # This account's own concurrent-runner cap; `None` = no per-account cap. Only a provider declaring
    # `per_account_runner_cap` carries one.
    max_runners: Optional[int] = None
    # The pinned node's uuid (the identifier) and its name (display only); both `None` when unpinned.
    node_id: Optional[str] = None
    node_name: Optional[str] = None
    five_hour_percent: Optional[int] = None
    weekly_percent: Optional[int] = None
    over_quota_spend_percent: Optional[int] = None
    quota_status: Optional[str] = None
    # Why the standing break verdict was given (the runner's sentence); None once it lapses or another reading wins.
    quota_status_detail: Optional[str] = None
    quota_updated_at: Optional[str] = None
    # When a node that held its session was deleted (`Account.node_removed_at`); `None` for none.
    node_removed_at: Optional[str] = None
    # Whose account this is, as a label (`app/tenancy/labels.py::OrgRef`): the operator's org chip and the
    # Change-org picker's "current org". `None` only for an id naming no `orgs` row.
    org: Optional[OrgRef] = None


def _validated_slug(value: Optional[str]) -> Optional[str]:
    """The slug grammar the wizard's `account_add` enforces (`app/routes/commands.py::_SLUG_RE`, imported).

    Without it any non-UI caller could create a slug no wizard command can address. `None` passes
    through: pydantic runs this on an explicit `null`, and `_SLUG_RE.match(None)` would raise TypeError,
    which pydantic does not turn into a 422."""
    if value is None:
        return None
    if not _SLUG_RE.match(value):
        raise ValueError(SLUG_RULE)
    return value


def _validated_auth_kind(value: Any) -> str:
    """The submitted `auth_kind`, checked against the vocabulary: a 422 naming the accepted values.

    App-side, not a pydantic `Literal`: a new kind should cost a constant, and a pydantic failure echoes
    the rejected input, on a body that also carries an API key. One rule for the whole body: nothing on
    it is pydantic-validated."""
    kind = value if isinstance(value, str) else ""
    if kind not in account_auth.AUTH_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                "auth_kind must be one of: " + ", ".join(account_auth.AUTH_KINDS)
                + f" (default {account_auth.AUTH_KIND_OAUTH})"
            ),
        )
    return kind


def _validated_api_key(value: Any, credential_type: str) -> str:
    """The submitted API key, trimmed and checked for storage hazards, or `""` for "not supplied".

    Never echoes the value. `_validated_value` is the Credentials page's own check for this field (length
    cap, NUL, type); a NUL matters because a failed JSONB INSERT would render the key into the log.
    `""` means "missing" on create and "leave unchanged" on update (`credentials.py::_merged_config`).
    `credential_type` is the provider's own (`AgentProvider.api_key_credential_type`); a provider with none
    falls back to Anthropic's spec, whose field rules are the same, so the key is still checked before the
    caller refuses it."""
    if value is None:
        return ""
    # Looked up by name in `app/credentials/registry.py`, so a second field on the spec cannot shift it.
    spec = CREDENTIAL_TYPES[credential_type or account_auth.API_KEY_CREDENTIAL_TYPE]
    field = next(f for f in spec.fields if f.key == account_auth.API_KEY_CONFIG_KEY)
    return _validated_value(spec, field, value).strip()


def _allowed_auth_kinds(
    session: Session, org_id, provider: Optional[agent_providers.AgentProvider], *, memo: Optional[dict] = None
) -> tuple[str, ...]:
    """The kinds of `provider` this org may create (`app/provider_auth_policy.py`); its supported kinds when
    none is allowed, so the default stays a kind the provider has and the create door names the refusal."""
    if provider is None:
        return ()
    allowed = provider_auth_policy.allowed(session, org_id, memo=memo)
    return tuple(k for k in provider.auth_kinds if (provider.value, k) in allowed) or provider.auth_kinds


def _validated_auth_kind_for(provider: Optional[agent_providers.AgentProvider], auth_kind: str) -> None:
    """422 unless `provider` accepts `auth_kind` (`AgentProvider.auth_kinds`): a DeepSeek OAuth account has
    no login to run, and an OAuth-only provider's API-key account has no key its CLI reads.

    An unknown provider (the vocabulary is open, `app/models/credentials.py::Account.provider`) keeps OAuth only: it
    names no credential type to store a key under."""
    accepted = provider.auth_kinds if provider is not None else (account_auth.AUTH_KIND_OAUTH,)
    if auth_kind in accepted:
        return
    who = provider.label if provider is not None else "this provider"
    raise HTTPException(
        status_code=422,
        detail=(
            f"{who} accounts cannot use auth_kind '{auth_kind}'; accepted: " + ", ".join(accepted)
            + ". Nothing was created."
        ),
    )


# Postgres INTEGER's maximum: the widest cap `accounts.max_runners` can store.
_MAX_RUNNERS_CEILING = 2**31 - 1


def _validated_max_runners(
    provider: Optional[agent_providers.AgentProvider], value: Optional[int], *, provider_value: str,
) -> Optional[int]:
    """The per-account runner cap, or `None` for "no cap". 422 when set on a provider that does not declare
    `per_account_runner_cap` (its spend is gated by quota instead), or when not a positive whole number.

    Checked here, not left to `ck_accounts_max_runners_positive`: the IntegrityError would be misread as a slug
    collision."""
    if value is None:
        return None
    if provider is None or not provider.per_account_runner_cap:
        who = provider.label if provider is not None else f"provider '{provider_value}'"
        raise HTTPException(
            status_code=422,
            detail=(
                f"max_runners is not accepted for {who} accounts: only a provider with no quota to gate "
                "on takes a per-account runner cap"
            ),
        )
    # The upper bound is the column's INTEGER range: past it the flush raises a DataError, a bare 500.
    if not 1 <= value <= _MAX_RUNNERS_CEILING:
        raise HTTPException(
            status_code=422,
            detail="max_runners must be a whole number of 1 or more, or null for no per-account cap",
        )
    return value


# One `GET` per registration or rotation; the operator is waiting on the form.
_PROBE_TIMEOUT_SECONDS = 5.0


def _probe_client() -> httpx.Client:
    """The client the key probe uses: the bounded, redirect-off policy (`app/http_bounded.py`). A seam the
    account tests monkeypatch, so no test call leaves the box."""
    return http_bounded.client(_PROBE_TIMEOUT_SECONDS)


def _probe_api_key(provider: Optional[agent_providers.AgentProvider], api_key: str, *, outcome: str) -> None:
    """Check `api_key` against its vendor BEFORE anything is written, when the provider declares a probe
    (`agent_providers.api_key_probe`); a no-op otherwise (Anthropic's create path is unchanged).

    401/403 -> 422 (the vendor rejected the key: never a silently enabled dead account). Anything else that
    is not a 2xx, or no answer at all -> 503 (could not verify; nothing written). The key travels only in the
    provider's own header (`Authorization` or Google's `x-goog-api-key`) and is never logged or echoed; the
    httpx exception, which carries the request and so the header, is dropped (`from None`), never chained or
    formatted. `outcome` names what the refusal left alone ("nothing was created" / "nothing was changed")."""
    probe = agent_providers.api_key_probe(provider.value) if provider is not None else None
    if provider is None or probe is None:
        return
    url, header, scheme = probe
    # Before any request is built: httpx would raise `UnicodeEncodeError` (not an `httpx.HTTPError`) holding
    # the whole `Bearer <key>` header value, on a key pasted with a smart quote or a zero-width space.
    if not _is_transmittable(api_key):
        raise HTTPException(
            status_code=422,
            detail=f"api_key contains characters an HTTP header cannot carry; {outcome}",
        )
    unreachable = HTTPException(
        status_code=503,
        detail=f"could not reach {provider.label} to verify the key; {outcome}",
    )
    try:
        with _probe_client() as client:
            # Streamed and never read: the status is the whole answer, so no body is buffered.
            value = f"{scheme} {api_key}" if scheme else api_key
            with client.stream("GET", url, headers={header: value}) as response:
                status = response.status_code
    except (httpx.HTTPError, ValueError):
        # `ValueError` covers an encoding failure the check above did not foresee; dropped, never chained.
        raise unreachable from None
    if status in (401, 403):
        raise HTTPException(
            status_code=422,
            detail=f"{provider.label} rejected this API key; {outcome}",
        )
    if not 200 <= status < 300:
        raise unreachable


# Neither body takes `config_dir` (derived per node); `extra='forbid'` 422s a client still sending one,
# the right direction for a value that tells a host what to delete.


class AccountCreateIn(StrictModel):
    """POST body — provision a new account RECORD. Quota fields are ingest-owned and rejected here."""

    # Absent -> drawn by the server (`app/account_slug.py`), the console's only way: a person names the
    # account with `name` and never picks the fleet-unique slug.
    slug: Optional[str] = None
    # Optional: `create_account` defaults it to the slug.
    name: Optional[str] = None
    # `''`, never `None`: NOT NULL with a `''` server default.
    description: str = ""
    enabled: bool = True
    # The column's own default. Not a closed set (`app/models/credentials.py::Account.provider`): an unknown value is a
    # row no adapter claims.
    provider: str = agent_providers.DEFAULT_PROVIDER
    node_id: Optional[str] = None
    # Absent -> `tenant_defaults.default_auth_kind`: OAuth where the org may use it, else the provider's
    # first allowed kind. Validated by `_validated_auth_kind`.
    auth_kind: Optional[str] = None
    # The operator's key, typed `Any`, not `Optional[str]`: a pydantic 422 echoes rejected input, so a
    # malformed key would come back in the response. Validated in handler code by `_validated_api_key`, as
    # `app/routes/credentials/` does for its whole body (property 2).
    #
    # On the create form rather than a separate credential step, so no live key sits in the broker attached
    # to nothing: one transaction, both halves roll back together.
    api_key: Any = None
    # Absent -> the provider's `auto_assign_default` (DeepSeek: off).
    auto_assign: Optional[bool] = None
    # The per-account runner cap; only for a provider declaring `per_account_runner_cap`.
    max_runners: Optional[int] = None
    # `personal` or `org` (`authz.validated_ownership`); absent is the caller's default — an administrator's
    # is `org`, and anyone else's account is always personal (G2).
    ownership: Optional[str] = None

    _check_slug = field_validator("slug")(_validated_slug)


def _publish_provider_echo(schema: dict) -> None:
    """Re-declare `provider` on `AccountUpdateIn`'s published schema, which the wrap validator would
    otherwise contradict.

    Without it OpenAPI declares the equal-echo body invalid while the route accepts it. A callable, not a
    dict: a `json_schema_extra` dict merges at the top level and would replace `properties` wholesale."""
    schema.setdefault("properties", {})["provider"] = {
        "type": "string",
        "description": "Accepted ONLY as an unchanged echo of the stored value, so a client may "
                       "round-trip the whole object; a CHANGE is refused with 422. Never settable — "
                       "see AccountCreateIn to set it.",
    }


class AccountUpdateIn(BaseModel):
    """PUT body — editable identity fields only (incl. enable/disable). `extra='forbid'` rejects any
    unknown or ingest-owned field (the quota columns) with a 422.

    The exception: `provider` is lifted out of the raw body by `_lift_provider_echo`, so it is neither
    rejected nor settable. `auth_kind` is declared and refused in the handler."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=lambda schema: _publish_provider_echo(schema))

    slug: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    node_id: Optional[str] = None
    # Declared only to be refused with a reason (pydantic's "extra inputs" 422 would not say why). An equal
    # value passes, so a full-object echo still works.
    auth_kind: Optional[str] = None
    # Rotation. `Any` for `AccountCreateIn.api_key`'s reason. Blank or absent means unchanged (the
    # Credentials page's rule for secret fields). The key cannot be read back or cleared: a keyless API-key
    # account is what the CHECK forbids.
    api_key: Any = None
    # As `enabled`: omitted means unchanged, an explicit null is refused.
    auto_assign: Optional[bool] = None
    # Null clears the cap; a number sets it (only for a provider declaring `per_account_runner_cap`).
    max_runners: Optional[int] = None
    # Two immutable-but-echoable fields, two mechanisms: `auth_kind` is declared (reachable from the setattr
    # loop; immutability rests on the handler check), `provider` is not a field at all (the loop cannot
    # reach it). Converging them belongs to `auth_kind`'s owner.
    # Not simply forbidden, because a client echoing the whole object must keep working; the wrap validator
    # lifts `provider` out before `extra='forbid'` and parks it for the handler.
    _echoed_provider: Optional[tuple] = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _lift_provider_echo(cls, data, handler):
        """Take `provider` off the raw body and remember it without declaring it a field.

        `mode='wrap'`: a before-validator can only return data (leaving the key updatable or 422), while wrap
        can remove it and write private state. A one-tuple, so `{"provider": null}` (a change, refused) is
        distinguishable from absent."""
        echoed = None
        if isinstance(data, dict) and "provider" in data:
            data = dict(data)
            echoed = (data.pop("provider"),)
        model = handler(data)
        model._echoed_provider = echoed
        return model

    @property
    def echoed_provider(self) -> Optional[tuple]:
        """`None` if the body named no provider, else a 1-tuple holding exactly what it sent."""
        return self._echoed_provider

    # A PUT can rename `slug`, so it is validated with the same function.
    _check_slug = field_validator("slug")(_validated_slug)



def _account_out(
    row: Account, reading: Optional[dict] = None, *, db_now: Optional[datetime] = None,
    node_name: Optional[str], actor_names: dict[UUID, str], org_labels: Mapping[UUID, OrgRef],
) -> AccountOut:
    """One roster row as the API returns it, with the quota read through the observation fold.

    `reading` is `account_fold.quota_reading_for`'s output (`None` when unobserved), threaded in so the
    list pays one query. The stored quota columns are not read (no writer; frozen).

    This page and the dispatch gate (`routes/fleet.py::_account_candidates`) must read the same values:
    `quota_reading_for` composes the reading and `account_fold.resolved_quota_status` the verdict,
    including that an expired verdict is no verdict. `db_now` is the DB clock (`None` leaves a verdict
    standing, the safe direction).

    `node_name`, `actor_names` and `org_labels` are pre-resolved by the caller and have no defaults, so a
    caller cannot silently serve every row as unpinned, unattributed or orgless. No `config_dir` is served (per node)."""
    observed = reading or {}
    quota_status = account_fold.resolved_quota_status(
        observed.get("quota_status"),
        stored=row.quota_status,
        until=row.quota_status_until,
        db_now=db_now,
    )
    standing = account_fold.standing_verdict(row.quota_status, row.quota_status_until, db_now=db_now)
    return AccountOut(
        **entity_fields(row, actor_names),
        slug=row.slug,
        # Straight off the row: a stored fact owned at creation.
        provider=row.provider,
        enabled=row.enabled,
        # The kind and the pointer, nothing behind it. As the one serializer for this table, this never loads
        # a key: `credential_id` is a uuid and the `Credential` relationship is not walked.
        auth_kind=row.auth_kind,
        credential_id=str(row.credential_id) if row.credential_id is not None else None,
        api_key_set=account_auth.is_api_key(row.auth_kind) and row.credential_id is not None,
        auto_assign=row.auto_assign,
        max_runners=row.max_runners,
        node_id=str(row.node_id) if row.node_id is not None else None,
        node_name=node_name,
        five_hour_percent=observed.get("five_hour_percent"),
        weekly_percent=observed.get("weekly_percent"),
        over_quota_spend_percent=observed.get("over_quota_spend_percent"),
        quota_status=quota_status,
        quota_status_detail=row.quota_status_detail if standing and standing == quota_status else None,
        quota_updated_at=iso_or_none(observed.get("quota_updated_at")),
        node_removed_at=iso_or_none(row.node_removed_at),
        org=org_ref_of(org_labels, row.org_id),
    )


class ProviderDegradationOut(BaseModel):
    """One itemized loss on the wire: `agent_providers.Degradation`, field for field."""

    model_config = ConfigDict(frozen=True)

    key: str
    capability: str
    consequence: str
    harness: bool


class AgentProviderOut(BaseModel):
    """One registrable agent CLI on the wire: a subset of `agent_providers.AgentProvider`'s fields, plus the
    roster's authorize-URL anchor.

    The console's provider list is rendered from this route only; the closed set, its order and each
    provider's degradations live in `apps/jaahd/agent_providers.py`, so console and control plane cannot disagree.

    Dispatch- and charter-side fields stay absent. Three public-vocabulary fields are served so the page
    never branches on a vendor name: `binary` and `needs_attested_binary` for the Add Account node picker
    (`Accounts.tsx::addLoginNodes`), and `has_quota_model` + `quota_windows` for the quota cells (PRD R5.3)."""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str
    # The executable the node attests on its PATH, matched against `NodeOut.runner_clis`.
    binary: str
    # Executables `binary` spawns itself (`AgentProvider.companions`): the node attests them beside it, and
    # `NodeOut.missing_companions` names the ones a node lacks. Never a login or dispatch gate.
    companions: list[str] = []
    # True => the credential is node-local and never on the shared store. False => it may be minted there.
    node_pinned: bool
    # True => a login is offered only on nodes attesting `binary` (the control plane 409s otherwise).
    needs_attested_binary: bool
    login_completion: str
    # What this provider's authorize URL must start with, or None when the roster has no opinion
    # (Claude). The browser applies it to the link it unseals: the control plane holds the
    # URL only as ciphertext, so the anchor `routes/commands.py` once applied to the echo has to move to
    # the one party that can read it. From `agent_providers.authorize_url_prefix`.
    authorize_url_prefix: Optional[str] = None
    # True => the unsealed URL must EQUAL the prefix (a device-flow page, where a prefix match would
    # admit a prefilled user-code tail); False => start with it. From `agent_providers.authorize_url_exact`.
    authorize_url_exact: bool = False
    # False => this vendor publishes no quota reading, ever; a blank or zero would both be lies (R5.3).
    has_quota_model: bool
    # The quota cells' labels, short window first (`agent_providers.AgentProvider.quota_windows`); one label
    # means there is no weekly window to show.
    quota_windows: list[str]
    # The auth kinds an account may declare (`AgentProvider.auth_kinds`): the dialog offers a login only
    # when `oauth` is among them and a key field only when `api_key` is.
    auth_kinds: list[str]
    # The kind the dialog starts on, and what a create that names none gets, for the caller's org
    # (`app/tenant_defaults.py::default_auth_kind`: OAuth where allowed, in any org).
    default_auth_kind: str
    # The subset of `auth_kinds` on the caller's org's provider grid (`app/provider_auth_policy.py`);
    # empty = this provider is not allowed at all.
    allowed_auth_kinds: list[str]
    # True => the dialog offers a per-account runner cap (`accounts.max_runners`).
    per_account_runner_cap: bool
    # What a new account's Auto-assign box starts at.
    auto_assign_default: bool
    degradations: list[ProviderDegradationOut]
    # `AgentProvider.capabilities` field for field (`_capabilities_out`): snake_case names, tuples as arrays,
    # `runtime` as `{auth_kind: "acp"|"legacy_only"|"acp_only"}`. What the console reads instead of a provider name;
    # `runtime_modes.supported` minus `.native` is the "not enforced natively" set.
    capabilities: dict[str, Any]


def _capabilities_out(provider: agent_providers.AgentProvider, *, itemized: bool) -> dict[str, Any]:
    """`provider.capabilities` on the wire. `gaps` are the row's `degradations`, withheld like them."""
    out = dataclasses.asdict(provider.capabilities)
    out["runtime"] = dict(provider.capabilities.runtime)
    if not itemized:
        out["gaps"] = []
    return out


# Declared before `/api/accounts/{account_id}`: FastAPI matches in declaration order, so otherwise this
# path would parse as an account id and 422.
@router.get("/api/accounts/providers", response_model=list[AgentProviderOut])
@feature("accounts", "view")
def list_agent_providers(
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[AgentProviderOut]:
    """The closed set of agent CLIs an account may be registered against, in catalogue order. The response
    changes when `app/agent_providers.py`'s `PROVIDERS` does, and `default_auth_kind`/`allowed_auth_kinds`
    with the caller's org (one read of `app/provider_auth_policy.py`'s verdict).

    Not superadmin-gated: it is vocabulary, not a row.

    Except `degradations`: the deployment's own account of its runner harness's soft edges, which an
    `accounts:view` tenant should not read. It is served only on the key that opens the account form
    (`system:create`), its only reader (`ProviderLimitations` in
    `apps/web/src/pages/credentials/AccountEditPage.tsx`); an empty list otherwise. This withholds it from a
    seat, not a tenant org: a tenant's own superadmin holds that key by construction. Empty is already
    "nothing to render" (Claude Code's list is legitimately empty)."""
    # Org-wide (`has_org`): the deployment's harness notes are not a fact about any one project.
    itemized = principal is None or principal.has_org(permission_key("system", "create"))
    caller_org = seam.bound_org() or _user.org_id
    allowed_memo: dict = {}
    return [
        AgentProviderOut(
            value=p.value,
            label=p.label,
            binary=p.binary,
            companions=list(p.companions),
            node_pinned=p.node_pinned,
            needs_attested_binary=p.needs_attested_binary,
            login_completion=p.login_completion,
            authorize_url_prefix=agent_providers.authorize_url_prefix(p.value) or None,
            authorize_url_exact=agent_providers.authorize_url_exact(p.value),
            has_quota_model=p.has_quota_model,
            quota_windows=list(p.quota_windows),
            auth_kinds=list(p.auth_kinds),
            default_auth_kind=tenant_defaults.default_auth_kind(
                _allowed_auth_kinds(session, caller_org, p, memo=allowed_memo)
            ),
            allowed_auth_kinds=[
                k for k in p.auth_kinds
                if provider_auth_policy.is_allowed(session, caller_org, p.value, k, memo=allowed_memo)
            ],
            per_account_runner_cap=p.per_account_runner_cap,
            auto_assign_default=p.auto_assign_default,
            degradations=[
                ProviderDegradationOut(key=d.key, capability=d.capability, consequence=d.consequence, harness=d.harness)
                for d in p.degradations
            ] if itemized else [],
            capabilities=_capabilities_out(p, itemized=itemized),
        )
        for p in agent_providers.PROVIDERS
    ]


@router.get("/api/accounts", response_model=list[AccountOut])
@feature("accounts", "view")
def list_accounts(
    _user: AdminUser = Depends(require_user),
    # Gated by the `accounts:view` key plus the scoped select; scoping reads this frozen
    # principal, not the ORM user row.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[AccountOut]:
    rows = session.scalars(readable_account_select(principal).order_by(Account.slug)).all()
    readings = account_fold.quota_readings(session)
    db_now = account_fold.db_clock(session)
    names = names_by_id(session, [row.node_id for row in rows])
    # Every actor id on the page resolved in one query.
    actors = actor_names_for(session, rows)
    orgs = org_refs(session, [row.org_id for row in rows])
    return [
        _account_out(row, readings.get(row.slug), db_now=db_now, node_name=names.get(row.node_id),
                     actor_names=actors, org_labels=orgs)
        for row in rows
    ]


@router.get("/api/accounts/{account_id}", response_model=AccountOut)
@feature("accounts", "view")
def get_account(
    account_id: UUID,
    _user: AdminUser = Depends(require_user),
    # Same gate and scoping as `list_accounts`.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AccountOut:
    # `readable_account_clause`, not a bare read: a row filtered out of the roster must not be readable
    # by id, and a 404 (not 403) does not confirm it exists.
    row = get_or_404(
        session, Account, account_id, detail=f"unknown account {account_id}", where=(readable_account_clause(principal),)
    )
    return _account_out(row, account_fold.quota_reading_for(session, row.slug),
                        db_now=account_fold.db_clock(session),
                        node_name=names_by_id(session, [row.node_id]).get(row.node_id),
                        actor_names=resolve_actor_names(
                            session, (row.created_by_id, row.updated_by_id)),
                        org_labels=org_refs(session, [row.org_id]))


def _account_ownership(
    session: Session, user: AdminUser, principal: Principal | None, body: AccountCreateIn
) -> str | None:
    """Who a created account belongs to, and what a non-administrator may ask for (G2).

    An administrator (`authz.creates_org_rows`, the one question every create door asks) chooses
    `personal` or `org` (default org). Anyone else creates a PERSONAL API-KEY account only: `org` is a 403,
    and so is an OAuth record: its credential dir is derived from (slug, node) alone, so a
    record-create would adopt whatever login a deleted account or a failed wizard left on the host — a
    non-administrator's OAuth account comes only from the wizard, which runs a fresh login. A node pin
    must name a node they can see (`authz.visible_node_clause`), with the same 404 an unknown name gets."""
    writer = creates_org_rows(principal)
    ownership = validated_ownership(body.ownership, may_create_org=writer)
    if writer:
        return ownership
    if not account_auth.is_api_key(body.auth_kind):
        raise HTTPException(
            status_code=403,
            detail=(
                "bring your own login with Add Account — a record without a key would adopt a login "
                "already on the host, which only an administrator may do"
            ),
        )
    if body.node_id:
        node_uuid = node_id_or_404(session, body.node_id)
        if not can_access(session, principal, "node", node_uuid, mode=ObjectAccess.READ):
            raise HTTPException(status_code=404, detail=f"unknown node '{body.node_id}'")
    return OWNERSHIP_PERSONAL


@router.post("/api/accounts", response_model=AccountOut, status_code=201)
@feature("accounts", "create")
def create_account(
    body: AccountCreateIn,
    # Not `require_account_writer`: anyone holding `accounts:create` (a Developer or Maintainer anywhere,
    # G2) brings their OWN account here. The administrator bar decides only whether it may be the org's
    # (`_account_ownership`).
    user: AdminUser = Depends(require_user),
    # For the owner stamp below, not scoping: a caller with no org role owns the account (and its API-key
    # credential), else neither is reachable by their author. As `credentials.py::create_credential`.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AccountOut:
    """Provision an account record and, for an API-key account, the `credentials` row holding its key.

    One transaction: the credential is flushed first (`accounts.credential_id` is an FK onto it, and
    `ck_accounts_api_key_has_credential` requires it), nothing commits until both flush, and any failure
    rolls back both, so no orphan credential holds a live key. An OAuth create writes no credential and
    refuses an `api_key`."""
    # A pending org adds no AI account (`app/org_gate.py`): the org the row will be stamped into, which is
    # the bound scope, else the caller's own (the operator plane binds none).
    account_org = seam.bound_org() or user.org_id
    require_org_active(session, account_org)
    if not body.slug:
        body.slug = new_account_slug()
    # Stripped here once, so the catalogue lookup and the stored column read the same value. Blank falls back.
    provider_value = (body.provider or "").strip() or agent_providers.DEFAULT_PROVIDER
    provider = agent_providers.by_value(provider_value)
    if body.auth_kind is None:
        body.auth_kind = tenant_defaults.default_auth_kind(_allowed_auth_kinds(session, account_org, provider))
    ownership = _account_ownership(session, user, principal, body)
    auth_kind = _validated_auth_kind(body.auth_kind)
    _validated_auth_kind_for(provider, auth_kind)
    # The org's provider grid.
    provider_auth_policy.require_allowed(session, account_org, provider_value, auth_kind)
    credential_type = provider.api_key_credential_type if provider is not None else ""
    api_key = _validated_api_key(body.api_key, credential_type)
    max_runners = _validated_max_runners(provider, body.max_runners, provider_value=provider_value)
    if account_auth.is_api_key(auth_kind) and not api_key:
        # Required: a keyless API-key account is undispatchable; caught here for a sentence, not an
        # IntegrityError.
        raise HTTPException(
            status_code=422,
            detail=(
                f"api_key is required when auth_kind is '{account_auth.AUTH_KIND_API_KEY}' — an "
                "API-key account authenticates with nothing else, so a row without one could never "
                "dispatch a runner"
            ),
        )
    if api_key and not account_auth.is_api_key(auth_kind):
        # Refused, not ignored: an operator who pasted a key into an OAuth form would otherwise get a 201 and
        # believe it stored.
        raise HTTPException(
            status_code=422,
            detail=(
                f"api_key is only accepted when auth_kind is '{account_auth.AUTH_KIND_API_KEY}'; this "
                f"request asked for '{auth_kind}', which authenticates with a per-node credential "
                "directory instead. The key was NOT stored — nothing was created. Resend with "
                f"auth_kind='{account_auth.AUTH_KIND_API_KEY}', or without the key."
            ),
        )
    # The community edition's account cap, under the org lock to this commit (`app/ce_caps.py`); before the
    # vendor probe, so a refused create sends the key nowhere.
    if edition.IS_CE:
        ce_caps.require_account_headroom(session, account_org)
    # The vendor's own verdict on the key, before any row is built: a refusal writes nothing.
    if api_key:
        _probe_api_key(provider, api_key, outcome="nothing was created")
    row = Account(
        slug=body.slug,
        # The slug is the default label; blank counts as absent (NOT NULL, no server default).
        name=(body.name or "").strip() or body.slug,
        description=body.description,
        # The one write this column takes (the PUT refuses a change). Stripped so the PUT's equal-echo
        # comparison of a stripped echo against the stored value keeps matching. Blank falls back.
        provider=provider_value,
        enabled=body.enabled,
        # Absent -> what the provider declares (an unknown one: the column default, on).
        auto_assign=(
            body.auto_assign if body.auto_assign is not None
            else provider.auto_assign_default if provider is not None else True
        ),
        max_runners=max_runners,
        # `shared` keeps its column default (false): a record-create mints nothing, so an old node keeps
        # reading the login already in its config dir.
        # The body carries the node uuid (a name is not an identifier). An unresolvable value 404s rather
        # than silently storing an unpinned row (the closed direction).
        node_id=node_id_or_404(session, body.node_id) if body.node_id else None,
        auth_kind=auth_kind,
    )
    # Before the flush, so the INSERT carries the operator's id rather than the `SYSTEM_ACTOR_ID` default.
    stamp_actor(row, user, created=True)
    # Personal or org account, written with the INSERT (`visible_account_clause`'s owner arm).
    stamp_owner_on_create(principal, row, ownership)
    if account_auth.is_api_key(auth_kind):
        credential = Credential(
            # Named for the account, derived: it sits on Directory > Secrets, and the RESTRICT FK refuses its
            # manual delete, so the name must point at the account to disable first.
            name=f"runner-account/{row.slug.lower()}",
            # The provider's own type: `_validated_auth_kind_for` admitted `api_key`, so it declares one.
            type=credential_type,
            description=(
                f"{CREDENTIAL_TYPES[credential_type].label} for the runner account '{row.slug}'. "
                "Created with that account and owned by it: the account's own row points at this one, "
                "a node reads it over its credential socket at dispatch, and deleting the account "
                "deletes this row with it. "
                "Rotate the key from the account's edit dialog, not here."
            ),
            # The only place the key is written: into a `credentials` config, never onto `accounts`.
            config={account_auth.API_KEY_CONFIG_KEY: api_key},
        )
        stamp_actor(credential, user, created=True)
        # The same owner as its account, before the INSERT.
        stamp_owner_on_create(principal, credential, ownership)
        # Inside the scrub guard: this is the one INSERT binding the key. The account insert stays outside so
        # its `IntegrityError` remains the duplicate-slug 409.
        with _scrubbed_write(session, "create"):
            session.add(credential)
            session.flush()
        # Set once the credential has an id; the CHECK would refuse the account INSERT without it.
        row.credential_id = credential.id
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        # Rolls back both inserts, so a duplicate slug takes the key back out with the account.
        session.rollback()
        if constraint_name(exc) == ce_caps.TRIGGER_CONSTRAINT:
            raise ce_caps.refusal("accounts") from exc
        raise _slug_collision(session, exc, body.slug, principal, user.org_id)
    emit_console_event(session, entity="account", id=row.id, action="upserted")
    session.commit()
    session.refresh(row)
    return _account_out(row, account_fold.quota_reading_for(session, row.slug),
                        db_now=account_fold.db_clock(session),
                        node_name=names_by_id(session, [row.node_id]).get(row.node_id),
                        actor_names=resolve_actor_names(
                            session, (row.created_by_id, row.updated_by_id)),
                        org_labels=org_refs(session, [row.org_id]))


# ── Export / import between control planes (`app/account_transfer.py`) ───────────────────────────────


class AccountExportIn(StrictModel):
    # `Any` for `AccountCreateIn.api_key`'s reason: a pydantic 422 would echo it. Required for an API-key
    # account only (an OAuth bundle seals nothing).
    passphrase: Any = None


class AccountImportIn(StrictModel):
    # The exported file's JSON, read by `account_transfer.header`; `Any`, as it holds the sealed key.
    bundle: Any = None
    passphrase: Any = None


class AccountImportOut(BaseModel):
    account: AccountOut
    # An OAuth import: the control plane held no token to carry, so the account has no login until someone
    # logs it in on a node (the Log in flow, which finds the row by slug).
    needs_login: bool


@router.post("/api/accounts/{account_id}/export")
@feature("accounts", "edit")
def export_account(
    account_id: UUID,
    body: AccountExportIn,
    # The administrator bar, as every roster write: the bundle carries a live key.
    user: AdminUser = Depends(require_account_writer),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The account as an export bundle: an API-key account's key opened and sealed again under `passphrase`,
    recorded as a `credential_read`; an OAuth account as its record alone."""
    row = get_or_404(
        session, Account, account_id, detail=f"unknown account {account_id}", where=(readable_account_clause(principal),)
    )
    api_key: Optional[str] = None
    passphrase: Optional[str] = None
    if account_auth.is_api_key(row.auth_kind):
        try:
            passphrase = account_transfer.validated_passphrase(body.passphrase)
        except account_transfer.BundleError as exc:
            raise HTTPException(status_code=422, detail=f"{exc}; nothing was exported") from None
        credential = session.get(Credential, row.credential_id) if row.credential_id else None
        if credential is None or not has_sealed_secret(credential.config or {}, account_auth.API_KEY_CONFIG_KEY):
            raise HTTPException(
                status_code=409,
                detail=f"account '{row.slug}' holds no API key to export — rotate its key first",
            )
        try:
            api_key = open_secret(credential, account_auth.API_KEY_CONFIG_KEY)
        except ValueError:
            # NodeVault-sealed, moved or tampered: this control plane cannot open it. Dropped, never chained.
            raise HTTPException(
                status_code=409,
                detail=f"account '{row.slug}' holds a key this control plane cannot open, so it cannot be "
                       "exported; nothing was exported",
            ) from None
        # Committed before the key leaves the process (`app/data_access_events.py`).
        data_access_events.record_committed(
            session, data_access_events.CREDENTIAL_READ,
            f"Credential {credential.name} exported with account {row.slug}",
            data={"name": credential.name, "credential_id": str(credential.id),
                  "credential_type": credential.type, "consumer": "account_export", "task_id": None},
            org_id=credential.org_id, user=user,
        )
    return account_transfer.build(
        provider=row.provider, auth_kind=row.auth_kind, name=row.name, description=row.description,
        max_runners=row.max_runners, api_key=api_key, passphrase=passphrase,
    )


@router.post("/api/accounts/import", response_model=AccountImportOut, status_code=201)
@feature("accounts", "create")
def import_account(
    body: AccountImportIn,
    # As `create_account`, which this is: ownership and the OAuth administrator bar are decided there.
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AccountImportOut:
    """Create an account from an export bundle through `create_account`, which seals the key under this
    control plane's own keys and probes it. A fresh slug, no node, Auto-assign off: an account arriving
    from elsewhere takes no task until someone opts it in. OAuth lands enabled but with no login anywhere,
    like any record whose sign-in has not run: it is enabled so the login's completion, which only enables
    a row pinned to the login's node (`commands.py`'s phase 2), has nothing left to switch on."""
    try:
        head = account_transfer.header(body.bundle)
        api_key = (
            account_transfer.opened_key(body.bundle, body.passphrase)
            if account_auth.is_api_key(head["auth_kind"]) else None
        )
    except account_transfer.BundleError as exc:
        raise HTTPException(status_code=422, detail=f"{exc}; nothing was imported") from None
    if api_key is None and body.bundle.get("key") is not None:
        raise HTTPException(
            status_code=422,
            detail=f"this export is '{head['auth_kind']}' yet carries a key; nothing was imported",
        )
    needs_login = api_key is None
    account = create_account(
        AccountCreateIn(
            name=head["name"], description=head["description"],
            provider=head["provider"], auth_kind=head["auth_kind"], api_key=api_key,
            max_runners=head["max_runners"], auto_assign=False,
        ),
        user=user, principal=principal, session=session,
    )
    return AccountImportOut(account=account, needs_login=needs_login)


@router.put("/api/accounts/{account_id}", response_model=AccountOut)
@feature("accounts", "edit")
def update_account(
    account_id: UUID,
    body: AccountUpdateIn,
    user: AdminUser = Depends(require_account_writer),
    # For `_slug_collision`'s roster probe only.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AccountOut:
    row = get_or_404(session, Account, account_id, detail=f"unknown account {account_id}")
    updates = body.model_dump(exclude_unset=True)
    # Both auth fields come out of `updates` first: `api_key` is not a column (left in, the setattr loop
    # would hang a live secret on the ORM instance), and `auth_kind` must never be written.
    posted_auth_kind = updates.pop("auth_kind", None)
    posted_api_key = updates.pop("api_key", None)
    # The kind is immutable: the two kinds leave different artifacts (a node dir vs a broker row), and
    # flipping would strand one and lack the other, so the account stops authenticating. Delete and
    # recreate instead. An equal value passes; an explicit `null` is dropped.
    if posted_auth_kind is not None and str(posted_auth_kind) != row.auth_kind:
        raise HTTPException(
            status_code=422,
            detail=(
                f"auth_kind is immutable: this account is '{row.auth_kind}' and the kinds are not "
                "interchangeable — an OAuth account has a credential directory provisioned on a node "
                "and an API-key account has none, so switching would strand one and leave the other "
                "missing. Delete this account and create it again under the kind you want."
            ),
        )
    if "node_id" in updates:
        # Name→uuid before the setattr loop. Null/empty means unpin; an unresolvable name 404s.
        updates["node_id"] = (
            node_id_or_404(session, updates["node_id"]) if updates["node_id"] else None
        )
    # An explicit null on NOT NULL `name`/`description` is dropped (means "leave alone"); left in, the flush
    # would fail and be misreported as a slug collision.
    for not_null_field in ("name", "description"):
        if updates.get(not_null_field, "") is None:
            del updates[not_null_field]
    # The booleans are refused on null, not dropped: nothing clears a checkbox to null, so a silent 200
    # would misreport what was stored.
    for switch in ("enabled", "auto_assign"):
        if switch in updates and updates[switch] is None:
            raise HTTPException(
                status_code=422,
                detail=f"{switch} cannot be null — the column is NOT NULL, so absence is not a state "
                       f"for it (docs/standards/coding-standards.md). Send true or false, or omit the "
                       f"field to leave it unchanged.",
            )
    # An operator enabling an account vouches for its credential, so it is no longer an unfinished sign-in's
    # row that a failed later login may drop (`durable/accounts.py::drop_abandoned_row`).
    if updates.get("enabled") is True and row.signed_in_at is None:
        updates["signed_in_at"] = datetime.now(timezone.utc)
    # A blank `name` renders unidentifiable while passing NOT NULL. Refused (an update cannot borrow
    # create's slug fallback); `description` may be blank.
    if "name" in updates:
        updates["name"] = str(updates["name"]).strip()
        if not updates["name"]:
            raise HTTPException(
                status_code=422,
                detail="account name cannot be blank — it is the label every console surface renders",
            )
    # A null or blank `slug` is refused, not dropped: the slug is the credential dir's name, and a
    # swallowed field would surface as a false slug-collision error.
    if "slug" in updates and not str(updates["slug"] or "").strip():
        raise HTTPException(
            status_code=422,
            detail="account slug cannot be null or blank — it names the credential directory on "
                   "every node; omit the field to leave it unchanged",
        )
    # The slug is immutable: the path is `{node.jaah_home}/auth/{slug}`, so a rename makes the account
    # undispatchable and aims a later deprovision at a nonexistent dir, whose no-op success strands the
    # real credentials. An equal echo is accepted.
    if "slug" in updates and str(updates["slug"]).strip() != row.slug:
        raise HTTPException(
            status_code=422,
            detail=f"account slug is immutable: {row.slug!r} names the credential directory already "
                   f"provisioned on every node, and renaming it would strand those credentials. "
                   f"Deprovision this account and add it again under the new slug.",
        )
    # `provider` is immutable too (equal echo accepted): each vendor's CLI writes its own credential file,
    # so relabelling would hand a runner the wrong vendor's credential; the fix is a fresh login. Read off
    # `echoed_provider`, never `updates`: the column is unreachable anyway; this only gives the reason.
    if body.echoed_provider is not None and str(body.echoed_provider[0]).strip() != row.provider:
        raise HTTPException(
            status_code=422,
            detail=f"account provider is immutable: {row.provider!r} names the vendor CLI whose login "
                   f"already wrote this account's credential, and relabelling it would point every "
                   f"reader at a login that was never performed. "
                   f"Deprovision this account and add it again under the new provider.",
        )
    # ── Rotation ──────────────────────────────────────────────────────────────────────────────────────
    #
    # A blank or absent key leaves the stored one alone (`credentials.py::_merged_config`'s rule): the form
    # can never show the key, so every unrelated save submits it blank. There is no way to clear it.
    provider = agent_providers.by_value(row.provider)
    if "max_runners" in updates:
        updates["max_runners"] = _validated_max_runners(
            provider, updates["max_runners"], provider_value=row.provider,
        )
    rotated_key = _validated_api_key(
        posted_api_key, provider.api_key_credential_type if provider is not None else "",
    )
    if rotated_key:
        if not account_auth.is_api_key(row.auth_kind):
            # OAuth has no credential row to rotate; refused, not ignored, as on create.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"this account is '{row.auth_kind}' and authenticates with a per-node credential "
                    "directory, so there is no stored API key to rotate. The key was NOT stored and "
                    "nothing was changed."
                ),
            )
        credential = session.get(Credential, row.credential_id) if row.credential_id else None
        if credential is None:
            # Unreachable through the schema (CHECK + RESTRICT) but possible from old or manual edits. 409: the
            # operator can fix it by recreating the account.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"account '{row.slug}' is an API-key account whose credential row is missing, so "
                    "there is nothing to rotate — recreate the account"
                ),
            )
        # The vendor's verdict on the NEW key before it replaces the old one: a rejected rotation changes nothing.
        _probe_api_key(provider, rotated_key, outcome="nothing was changed")
        # Reassigned, never mutated in place (SQLAlchemy does not track plain JSONB mutation), and merged onto
        # the stored config per `_merged_config`'s per-key contract.
        credential.config = {**(credential.config or {}), account_auth.API_KEY_CONFIG_KEY: rotated_key}
        # Authorship survives; only `updated_by_id` moves.
        stamp_actor(credential, user)
        # Flushed inside the scrub guard: the config-bearing UPDATE. The account's own flush stays outside for
        # the 409. One transaction, so nothing half-rotates.
        with _scrubbed_write(session, "rotate the API key for"):
            session.flush()
    for field_name, value in updates.items():
        setattr(row, field_name, value)
    # After the field updates, without `created=True`, so authorship survives.
    stamp_actor(row, user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        # `updates`, not `row.slug` (expired by the rollback); no slug in the request is not a slug collision.
        raise _slug_collision(session, exc, updates.get("slug"), principal, user.org_id)
    emit_console_event(session, entity="account", id=row.id, action="upserted")
    session.commit()
    session.refresh(row)
    return _account_out(row, account_fold.quota_reading_for(session, row.slug),
                        db_now=account_fold.db_clock(session),
                        node_name=names_by_id(session, [row.node_id]).get(row.node_id),
                        actor_names=resolve_actor_names(
                            session, (row.created_by_id, row.updated_by_id)),
                        org_labels=org_refs(session, [row.org_id]))


def _drop_owned_api_key_credential(
    session: Session, *, auth_kind: str, credential_id: Optional[UUID]
) -> None:
    """Delete the `credentials` row an API-key account owned, once that account's own row is gone.

    Left behind it would be a live, billable key attached to nothing. Only if this account was the last
    reference: `accounts.credential_id` is `ON DELETE RESTRICT`, so it is checked explicitly.

    In a savepoint, because `node_groups.credential_id` also RESTRICTs, so the delete can fail today; the
    savepoint confines that to the credential, the account delete stands, and the key is left for manual
    removal. Values are passed in because the caller has already deleted the row. No-op for OAuth."""
    if credential_id is None or not account_auth.is_api_key(auth_kind):
        return
    # The account's DELETE must reach the DB before the reference count, or it counts itself.
    session.flush()
    still_referenced = session.scalar(
        select(func.count()).select_from(Account).where(Account.credential_id == credential_id)
    ) or 0
    if still_referenced:
        return
    credential = session.get(Credential, credential_id)
    if credential is None:
        return
    try:
        with session.begin_nested():
            session.delete(credential)
    except IntegrityError:
        # Swallowed deliberately (the savepoint undid the credential delete; re-raising would 500 a completed
        # deprovision). Not logged, as a second line of defence behind `hide_parameters=True`.
        return


@router.delete("/api/accounts/{account_id}", status_code=204)
@feature("accounts", "delete")
def delete_account(
    account_id: UUID,
    _user: AdminUser = Depends(require_account_writer),
    session: Session = Depends(get_session),
) -> Response:
    """Delete the roster row only; the on-disk credential dir is left untouched, on every node.

    The account becomes undispatchable while its login stays on the host. The console uses deprovision
    for an account with credentials; this is "Delete record" for a row with none
    (`status: "no_credentials"` in `GET /api/resources`).

    For an API-key account this is the full removal: the secret is the `credentials` row, dropped under
    `_drop_owned_api_key_credential`'s rules."""
    row = get_or_404(session, Account, account_id, detail=f"unknown account {account_id}")
    # Read before the delete, not off a deleted instance.
    auth_kind, credential_id = row.auth_kind, row.credential_id
    session.delete(row)
    _drop_owned_api_key_credential(session, auth_kind=auth_kind, credential_id=credential_id)
    emit_console_event(session, entity="account", id=account_id, action="deleted")
    session.commit()
    return Response(status_code=204)


@router.post("/api/accounts/{account_id}/remove", status_code=204)
@feature("accounts", "delete")
async def remove_account(
    account_id: UUID,
    user: AdminUser = Depends(require_account_writer),
    session: Session = Depends(get_session),
) -> Response:
    """The console's account Delete: stop the tasks running on the account, remove its token from its shared
    folder and its credential dir from every node that holds it, then delete the roster row.

    The CP decides; the nodes carry it out. Each node kills every session using the account and force-removes its
    files (`apps/jaahd/accounts.py`). A node that is offline or fails owes the removal (`app/account_removal.py`)
    and gets it again on its next connect, so the answer names nothing left behind. A stopped task keeps its slot
    and relaunches on another account (the row's delete clears its pins). The node-side steps are the
    offboarding's own (`app/tenancy/offboard.py`). An API-key account has nothing on a node and takes
    `delete_account`'s branch."""
    row = get_or_404(session, Account, account_id, detail=f"unknown account {account_id}")
    auth_kind, credential_id = row.auth_kind, row.credential_id
    if not account_auth.is_api_key(auth_kind):
        from app import account_removal, node_bus
        from app.routes.tasks.slots import kill_runner_keeping_hold, send_minted_intents
        from app.tenancy import offboard

        issued: list = []
        slug = row.slug
        # Out of rotation first, with its runners' kills, so no relaunch picks it while the nodes remove it. Read
        # before the row's delete nulls the slots' account columns.
        row.enabled = False
        row.auto_assign = False
        for task_id in _tasks_running_on(session, row.id):
            issued += kill_runner_keeping_hold(task_id, session, reason=f"account {slug} deleted", fresh_decision=True)[1]
        session.commit()
        send_minted_intents(issued)
        ref = offboard.account_ref(session, row.org_id, slug, row.node_id, bool(row.shared))
        connected = set(await node_bus.registry.connected_nodes_async())
        owed: list[tuple[UUID, str, dict[str, str]]] = []
        # The share first: a leaf there is every attached node's way back to this login. An account no node
        # holds and no login recorded on a share never minted a token there.
        if ref.dirs or row.shared:
            await offboard.delete_shared_leaves(session, [ref], connected, user.email, owed)
        # An offline node is owed the removal, not dispatched to: the bus would only refuse it.
        online = [d for d in ref.dirs if str(d[0]) in connected]
        owed += [(node_id, "account_delete", {"config_dir": config_dir})
                 for node_id, _name, config_dir in ref.dirs if str(node_id) not in connected and config_dir]
        await offboard.delete_account_dirs(session, [dataclasses.replace(ref, dirs=tuple(online))], user.email, owed)
        # In node order: `owe` locks each node row, and two Deletes must not take them in opposite orders.
        for node_id, verb, args in sorted(owed, key=lambda o: str(o[0])):
            account_removal.owe(session, node_id, verb, args, user.email)
        if row.node_id is not None and all(d[0] != row.node_id for d in ref.dirs):
            # The read seam hides another org's nodes (an operator acting as the account's org): nothing can be
            # sent or owed there, so it is logged rather than silently skipped.
            log.warning("account %s deleted; its pinned node %s is not readable here, so its copy stays", slug, row.node_id)
        # Every node's observation of the slug (unique fleet-wide), so none lists an account that is gone.
        session.execute(delete(AccountObservation).where(AccountObservation.slug == row.slug))
    session.delete(row)
    _drop_owned_api_key_credential(session, auth_kind=auth_kind, credential_id=credential_id)
    emit_console_event(session, entity="account", id=account_id, action="deleted")
    session.commit()
    return Response(status_code=204)


def _tasks_running_on(session: Session, account_id: UUID) -> list[int]:
    """The tasks holding a slot whose runner uses ACCOUNT_ID — sensed there, or leased to it
    (`account_runners.fleet_assigned_runner_counts`' account)."""
    from app import slot_state
    from app.models.worktrees import Worktree

    slot_account = func.coalesce(Worktree.account_id, Worktree.leased_account_id)
    return sorted({
        task_id for task_id in session.scalars(
            select(Worktree.task_id).where(Worktree.state == slot_state.TASK, slot_account == account_id)
        ) if task_id is not None
    })


def deprovision_consequence(slug: str) -> str:
    """What the two deprovision doors tell a tenant did not happen when the node's home is unrecorded,
    spelled once (also read by `tests/test_slot_tenant_copy.py`)."""
    return f"the credential dir for account {slug!r} cannot be resolved and nothing was deprovisioned"


@router.post("/api/accounts/{account_id}/deprovision", status_code=204)
@feature("accounts", "delete")
async def deprovision_account(
    account_id: UUID,
    user: AdminUser = Depends(require_account_writer),
    # For the unpinned fallback only: `default_dispatch_node` probes `can_access` with a `Principal`.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Fully deprovision an account: delete its on-disk credential dir on the node the row names, then, only
    if that host confirms, delete the roster row. 204 on success. An API-key account takes its own branch
    at the top (no host leg).

    Credentials off disk first, row second: a crash between leaves a visible row over an empty dir; the
    reverse would strand credentials with no record (`apps/jaahd/accounts.py::_account_delete` keeps the
    same invariant).

    Scope is one node: the pin, else the CP's default node; the path is derived from that node. A dir
    materialized on several nodes needs the per-node route for each.

    A host failure is 502 and keeps the row. An already-absent dir is success (idempotent).

    Dispatch runs through `app/durable/accounts.py::retire` synchronously; this route writes the
    `command_audit_log` row itself. A new route, not a flag on DELETE: under deploy skew an old API would
    404 (fail closed) rather than silently drop the flag."""
    row = get_or_404(session, Account, account_id, detail=f"unknown account {account_id}")
    if account_auth.is_api_key(row.auth_kind):
        # No host leg for an API-key account: it holds no credential on any host, and the 409 below would
        # give advice that could never work. The row and its `credentials` row go, under `delete_account`'s
        # rule; the token-less home a node derived for it (plugins, transcripts) is NOT removed here.
        auth_kind, credential_id = row.auth_kind, row.credential_id
        session.delete(row)
        _drop_owned_api_key_credential(session, auth_kind=auth_kind, credential_id=credential_id)
        emit_console_event(session, entity="account", id=account_id, action="deleted")
        session.commit()
        return Response(status_code=204)
    slug = row.slug
    # One read of the pin yields both the dispatch target (its uuid — bus routing and the audit row key on
    # it) and the home (path source), so they cannot be about different nodes.
    pinned = session.get(Node, row.node_id) if row.node_id is not None else None
    if pinned is not None:
        node_id = str(pinned.id)
        node_label = pinned.name
        jaah_home = pinned.jaah_home
    else:
        # Per plane: for a tenant, `default_dispatch_node` answers a node of the caller's own org (or a 409);
        # otherwise the CP's own node would receive a tenant's delete with a path from the operator's home.
        # Off the loop: `default_dispatch_node` reads the DB (`node_bus.registry.is_connected`), never
        # on the event loop.
        node_id = await run_in_threadpool(default_dispatch_node, session, principal)
        # Matched through `node_matches`: the node uuid (`app/node_identity.py`). The name only labels the
        # refusals below.
        home_row = session.execute(select(Node.jaah_home, Node.name).where(node_matches(node_id))).first()
        jaah_home = home_row.jaah_home if home_row else None
        node_label = home_row.name if home_row else node_id
    config_dir = resolve_auth_dir(jaah_home, slug)
    if not config_dir:
        # The highest-consequence line here: `resolve_auth_dir` fails closed to `""`, which is not a path.
        # Refused before dispatch, naming the undeclared node; guessing a default would delete the wrong
        # directory silently. Tenants read it via `tenancy/copy.py::node_home_unrecorded`.
        if is_tenant_org(user.org_id):
            raise HTTPException(
                status_code=409,
                detail=node_home_unrecorded(node_label, deprovision_consequence(slug)),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"node '{node_label}' has no declared Jaah home, so the credential dir for account "
                f"'{slug}' cannot be resolved — nothing was deprovisioned; set the node's Jaah home "
                "and retry"
            ),
        )

    # Dispatch failures raise HTTPException (502/503/504) from the workflow; propagated untouched so the
    # row survives with the real reason. Durable, so a CP restart resumes; the node list is for
    # the per-node sibling. `run_in_threadpool`: the steps are synchronous and the serving loop must not block.
    from app.durable import accounts as durable_accounts

    # The audit row is written here because the durable step calls bare `_dispatch` (auditing there would
    # repeat per replay). Every outcome is recorded, as `dispatch_and_audit` does: a reply as itself, a
    # raised dispatch as `"dispatched"` with the reason, then re-raised. Through the usual scrubbers.
    audit_args = _credential_safe_args("account_delete", {"config_dir": config_dir})
    try:
        reply = await run_in_threadpool(durable_accounts.retire, slug, config_dir, [node_id])
    except HTTPException as exc:
        _record_audit(
            session, node_id, "account_delete", audit_args, "dispatched", {"reason": exc.detail}, user.email,
        )
        raise
    status = str(reply.get("status") or "failed")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    _record_audit(
        session, node_id, "account_delete", audit_args, status,
        _credential_safe_result("account_delete", result), user.email,
    )
    if status != "done":
        reason = str(result.get("stderr") or result.get("reason") or "").strip()
        raise HTTPException(
            status_code=502,
            detail=(
                f"host failed to remove credentials at {config_dir}"
                + (f": {reason}" if reason else "")
                + " — account row kept"
            ),
        )

    # Credentials are gone; drop the row. The observe pass builds from the DB roster, so nothing re-inserts it.
    session.delete(row)
    emit_console_event(session, entity="account", id=account_id, action="deleted")
    session.commit()
    return Response(status_code=204)


@router.post("/api/accounts/{account_id}/nodes/{node_id}/deprovision", status_code=204)
@feature("accounts", "delete")
async def deprovision_account_on_node(
    account_id: UUID,
    node_id: str,
    user: AdminUser = Depends(require_account_writer),
    session: Session = Depends(get_session),
) -> Response:
    """Deprovision one account on one named node: delete its credential dir there, then drop the roster row
    only if no other node still holds the account. 204 on success.

    A login is per (account, node), so its removal is too. A new route rather than a field on the
    account-level one: under deploy skew an old API dropping the field would deprovision the pinned node
    while the console reported the clicked one (fail open); a 404 fails closed.

    Same ordering: credentials first, roster second. The row survives while another node holds the
    account, and a pin naming the cleared node is repointed at one that still holds it. An API-key account
    is held by no node and takes its own branch after the node 404."""
    row = get_or_404(session, Account, account_id, detail=f"unknown account {account_id}")
    # `node_id` is the path's uuid (bus key, 502 text); resolved once, fail-closed: an unresolvable node
    # would make `remaining` empty and delete the row on an unregistered node.
    node_uuid = node_id_or_404(session, node_id)
    if account_auth.is_api_key(row.auth_kind):
        # No node holds an API-key account (see the account-level branch), so this is the last deprovision and
        # the row goes, as an empty `remaining` would decide. The observation delete is skipped too. The node 404
        # still runs: naming an unregistered node is a caller bug.
        auth_kind, credential_id = row.auth_kind, row.credential_id
        session.delete(row)
        _drop_owned_api_key_credential(session, auth_kind=auth_kind, credential_id=credential_id)
        emit_console_event(session, entity="account", id=account_id, action="deleted")
        session.commit()
        return Response(status_code=204)

    # Derived for this node, read off the uuid just resolved, so path and 404 are about the same row. The name
    # only labels the refusals below.
    jaah_home, node_label = session.execute(
        select(Node.jaah_home, Node.name).where(Node.id == node_uuid)
    ).one()
    config_dir = resolve_auth_dir(jaah_home, row.slug)
    if not config_dir:
        # Fail closed before dispatch (see `deprovision_account`): success here also deletes the observation
        # and maybe the roster row, so an empty-path "success" would erase the record of live credentials.
        # Tenant wording: `tenancy/copy.py::node_home_unrecorded`.
        if is_tenant_org(user.org_id):
            raise HTTPException(
                status_code=409,
                detail=node_home_unrecorded(node_label, deprovision_consequence(row.slug)),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"node '{node_label}' has no declared Jaah home, so the credential dir for account "
                f"'{row.slug}' cannot be resolved — nothing was deprovisioned; set the node's Jaah "
                "home and retry"
            ),
        )

    # Dispatch failures propagate untouched: nothing may be deleted, and the caller needs the real reason.
    reply, _audit_id = await dispatch_and_audit(
        session, node_id, "account_delete", {"config_dir": config_dir}, user.email,
    )
    if str(reply.get("status") or "failed") != "done":
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        reason = str(result.get("stderr") or result.get("reason") or "").strip()
        raise HTTPException(
            status_code=502,
            detail=(
                f"host {node_label} failed to remove credentials at {config_dir}"
                + (f": {reason}" if reason else "")
                + " — nothing deleted"
            ),
        )

    # Drop this node's observation now so it stops listing an account it just deleted.
    session.execute(
        AccountObservation.__table__.delete().where(
            AccountObservation.node_id == node_uuid, AccountObservation.slug == row.slug,
        )
    )
    # Ordered by name for a predictable re-pin tie-break; the selected value is the uuid.
    remaining = session.scalars(
        select(AccountObservation.node_id)
        .join(Node, Node.id == AccountObservation.node_id)
        .where(AccountObservation.slug == row.slug, AccountObservation.node_id != node_uuid)
        .order_by(Node.name)
    ).all()
    if remaining:
        if row.node_id == node_uuid:
            row.node_id = remaining[0]
            # Stamped: re-pinning is an operator mutation, and the trigger moves `updated_at` regardless.
            stamp_actor(row, user)
        emit_console_event(session, entity="account", id=account_id, action="upserted")
    else:
        session.delete(row)
        emit_console_event(session, entity="account", id=account_id, action="deleted")
    session.commit()
    return Response(status_code=204)
