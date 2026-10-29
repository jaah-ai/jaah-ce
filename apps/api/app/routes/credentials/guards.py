"""Credential authz and NodeVault guards — the scoped row lookup, the edit test and every `_refuse_*` refusal."""
from __future__ import annotations

from typing import Any, Mapping, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.authz.catalog import permission_key
from app.authz.principal import Principal
from app.authz.visibility import (
    edits_any_credential,
    scoped_credential_select_for,
    sees_system_credentials,
)
from app.credentials.registry import (
    CredentialTypeSpec,
    sealing_destination,
    secret_keys,
)
from app import credential_consumers, nodevault_policy
from app.credential_reseal import stored_secret_envelope_exists
from app.credential_crypto import (
    NodeVaultInvariantError,
    SealedSecret,
    check_nodevault_invariant,
    is_nodevault_key_id,
)
from app import http_bounded
from app.credentials.oauth_wizards import ConnectProviderSpec
from app.models.credentials import Credential
from app.models.orgs import Org


# Each route's declared key, rendered once via `permission_key`. No `credentials:create` constant: a
# create has no existing row to scope.
_VIEW_KEY = permission_key("credentials", "view")
_EDIT_KEY = permission_key("credentials", "edit")
_DELETE_KEY = permission_key("credentials", "delete")


def _refuse_a_broken_nodevault_invariant(config: Mapping[str, Any], nodevault: bool) -> None:
    """422 unless every sealed secret of the write's FINAL config (posted and kept alike) is under the
    org's NodeVault key exactly when the row is a NodeVault credential. Names the field, never a value."""
    try:
        check_nodevault_invariant(config, nodevault=nodevault)
    except NodeVaultInvariantError as exc:
        raise _nodevault_refusal(exc) from None


def _nodevault_refusal(exc: NodeVaultInvariantError) -> HTTPException:
    """The one 422 for a NodeVault refusal (wire or invariant); its text names a field, never a value."""
    return HTTPException(status_code=422, detail=str(exc))


def _not_in_plan() -> HTTPException:
    """The plan gate's refusal (D11, AC-1.3): no plan term, or the plan dropped NodeVault after setup (frozen)."""
    return HTTPException(status_code=403, detail="NodeVault isn't included in your plan")


def _ineligible(status_code: int, reason: str) -> HTTPException:
    """A NodeVault eligibility refusal whose sentence `app/credential_consumers.py` owns (type or consumers)."""
    return HTTPException(status_code=status_code, detail=reason)


def _refuse_ineligible_nodevault(
    session: Session, spec: CredentialTypeSpec, credential: Credential, config: Mapping[str, Any]
) -> None:
    """422 when the type (or this row's OAuth tokens) may not be NodeVault (D7); 409 naming every control-plane
    consumer of `credential` under its final `spec` and `config` (D1, AC-5.3)."""
    reason = credential_consumers.oauth2_reason(spec.slug, config)
    if reason is not None:
        raise _ineligible(422, reason)
    consumers = credential_consumers.consumers_of_row(session, credential, config, type_slug=spec.slug)
    if consumers:
        raise _ineligible(409, credential_consumers.consumers_reason(consumers))


def _org_of(session: Session, org_id: Optional[UUID]) -> Optional[Org]:
    return session.get(Org, org_id) if org_id is not None else None


def _refuse_turning_nodevault_on(
    session: Session, org_id: Optional[UUID], spec: CredentialTypeSpec, credential: Credential,
    config: Mapping[str, Any],
) -> None:
    """Refuse a credential BECOMING NodeVault (create with `nodevault=true`, or a PATCH turning it on).

    In order: the plan gate (403 `_not_in_plan` when not allowed, frozen included; 409 when not set
    up), the type (422, D7), then every control-plane consumer (409 naming them, D1/AC-5.3). `credential` is
    the row, or a transient one (no id: a new row has no pointers) carrying the create's type, org and
    creator; `config` is what the write will store. Before the config is merged, so the reason is the plan's,
    not a key's."""
    org = _org_of(session, org_id)
    policy = nodevault_policy.status(session, org) if org is not None else None
    if policy is None or not policy.allowed:
        raise _not_in_plan()
    if not policy.setup:
        # Allowed, but the org has no `current` key yet, so there is nothing to seal under.
        raise HTTPException(status_code=409, detail="NodeVault isn't set up")
    _refuse_ineligible_nodevault(session, spec, credential, config)


def _refuse_kept_nodevault_secrets(config: Mapping[str, Any]) -> None:
    """Turning NodeVault OFF (AC-5.8): every secret must be re-entered, sealed under KMS in the browser, since
    no path hands a NodeVault plaintext back to the control plane. 422 naming the fields still under NodeVault."""
    kept = sorted(
        key for key, value in config.items()
        if isinstance(value, SealedSecret) and is_nodevault_key_id(value.key_id)
    )
    if kept:
        raise HTTPException(
            status_code=422,
            detail=f"Re-enter every secret to turn NodeVault off (still under NodeVault: {', '.join(kept)})",
        )


def _refuse_kept_secrets_if_destination_moved(
    row: Credential, spec: CredentialTypeSpec, stored: dict[str, Any], config: dict[str, Any]
) -> None:
    """400 when an update moves the row's sealing destination while keeping a stored secret.

    Every secret is bound to the URL it was sealed for (`app/credential_crypto.py::tier2_aad`), so a
    kept envelope beside a new URL could never be opened: refusing at the write, naming the field first
    (`CredentialEditPage.tsx` re-homes it), beats a credential that silently stops working. Nothing here
    opens anything — a kept secret is the SAME object `_merged_config` carried over from `stored`.
    """
    if sealing_destination(row.type, stored) == sealing_destination(spec.slug, config):
        return
    for key in sorted(secret_keys(spec.slug, config)):
        kept = config.get(key)
        if isinstance(kept, SealedSecret) and kept is stored.get(key):
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be re-entered because the URL it was sealed for changed",
            )


def _may_edit(principal: Principal | None, row: Credential) -> bool:
    """Would `principal`'s `PATCH /api/credentials/{row.id}` be admitted? The by-id read's `editable`.

    Restates `update_credential`'s rule (`scoped_credential_select_for(principal, _EDIT_KEY)`) for a row
    already read through a scoped select (`authz.credential_write_clause`): any row for an operator, acting
    or on its own plane (`edits_any_credential`; another org's row is saved acting as that org); else the
    caller's own row, or an ORG row (`owner_id IS NULL`) for an ORG-WIDE holder of the edit key (`has_org`).
    Someone else's personal credential is otherwise never editable, a tenant Org Admin's included."""
    if principal is None or edits_any_credential(principal):
        return True
    if row.system and not sees_system_credentials(principal):
        return False
    return row.owner_id == principal.user_id or (principal.has_org(_EDIT_KEY) and row.owner_id is None)


def _refuse_replayed_stored_envelopes(session: Session, posted: dict[str, SealedSecret]) -> None:
    """400 when an inline envelope IS one the table stores, for that field, in ANY row. The console
    never re-posts a stored envelope (the API never serves one), so a match is a copy lifted from the
    store (a DB read, a dump); opening it inline would spend the stored secret against its host without
    the audit a fill writes — and against a row the caller may not even see, which is why the check is
    TABLE-WIDE rather than on the resolved row.

    The probe itself lives with the column's other raw read (`credential_reseal.stored_secret_envelope_exists`,
    unscoped by design and registered as such). Names the field only.
    """
    for key, value in posted.items():
        if stored_secret_envelope_exists(session, key, value.envelope):
            raise HTTPException(
                status_code=400,
                detail=f"{key} is the stored secret; leave it blank to test with the stored value",
            )


def _scoped_row_or_404(
    session: Session, principal: Principal | None, credential_id: UUID, key: str
) -> Credential:
    """The only id lookup in this module — every route reaches a row through it, read and write alike.

    404, not 403 (`app/authz/object_access.py::require_object_access`): a 403 would confirm existence. Writes scope
    through it too, so a delegable `credentials:edit`/`:delete` never reaches ungranted rows. `key` is the
    calling route's own declared key: `scoped_credential_select_for` narrows a caller lacking it to rows
    they brought, so a `credentials:view` holder reads every granted row and PATCHes only their own.

    When a write-key lookup misses but the row is readable under `_VIEW_KEY` (the caller's own GET
    answers from that select), the answer is a 403 naming the missing key: the caller has already seen
    the row, so there is no oracle. The guard-side twin is `app/authz/object_access.py::readable_write_denial`."""
    row = session.scalar(
        scoped_credential_select_for(principal, key).where(Credential.id == credential_id)
    )
    if row is None and key != _VIEW_KEY:
        readable = session.scalar(
            scoped_credential_select_for(principal, _VIEW_KEY).where(Credential.id == credential_id)
        )
        if readable is not None:
            raise HTTPException(
                status_code=403,
                # Verb-neutral (PATCH, DELETE, connect): names the key, not the act.
                detail=f"{key} required — you can read this credential but not change it; ask an operator",
            )
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown credential {credential_id}")
    return row


def _refuse_a_caller_supplied_endpoint_without_the_edit_key(
    provider: ConnectProviderSpec, token_endpoint: str, principal: Principal | None
) -> None:
    """Refuse a connect whose token endpoint came off the row when it fails the destination rule, or when
    the caller lacks `credentials:edit`.

    `own_credential=True` must not widen this: for the generic provider the row supplies the endpoint,
    so completing makes this deployment POST to a caller-chosen host — the capability `POST /test` is not
    widened for. The destination first (`http_bounded.public_destination_refusal`, the rule
    `oauth2_tokens.access_token_for` dials by, the same for every org), then the capability check.
    """
    if provider.token_url is not None:
        return
    refusal = http_bounded.public_destination_refusal(token_endpoint)
    if refusal:
        # "Token URL" is the registry label the page shows.
        raise HTTPException(status_code=422, detail=f"Token URL refused: {refusal}.")
    # Org-wide (`has_org`): a key held on one project does not let its holder aim the CP's egress.
    if principal is None or principal.has_org(_EDIT_KEY):
        return
    # The capability refusal, operator plane only; worded for both readers.
    raise HTTPException(
        status_code=403,
        detail=(
            "This credential's Token URL is one you supplied, so connecting it calls a host you "
            "chose. That needs the credentials:edit permission."
        ),
    )
