"""The on-demand credential test — `POST /api/credentials/test` and the helpers that open and pin its secrets."""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import scoped_credential_select_for
from app.db import get_session
from app.entity_shape import is_blank
from app.credentials import health as credential_health
from app.credentials.registry import (
    CredentialTypeSpec,
    sealing_destination,
    secret_keys,
)
from app.credential_crypto import (
    SealedSecret,
    is_nodevault_key_id,
    open_sealed,
    open_secret,
    sealed_secret_from_wire,
)
from app.credentials.testers import (
    TESTERS,
    TestResult,
    _effective_base_url,
    _operator_host_field,
    _same_origin,
    new_deadline,
    nodevault_untestable_result,
    request_origin,
    unsupported_result,
    withheld_secret_result,
)
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.tenancy.copy import operator_hosted
from app.tenancy.ids import is_tenant_org
from app.user_activity import record_write
from app.routes.credentials.guards import _EDIT_KEY, _refuse_replayed_stored_envelopes, _scoped_row_or_404
from app.routes.credentials.credential_wire import (
    _VALUE_MAX_LEN,
    _connect_out,
    _optional_uuid,
    _scrubbed_write,
    _untestable_reason_out,
    _validated_posted_config,
    _validated_type,
)


router = APIRouter(tags=["credentials"])


class CredentialTestOut(BaseModel):
    """One completed connection test, mirroring `credentials.testers.TestResult`. `ok` is True only for
    `status == "success"`, so `unsupported` never reads as green.

    Nothing echoes the posted config; `detail` is at most an HTTP status or a fixed transport token.
    """

    ok: bool
    # Pinned to the three states so a drifted status fails here.
    status: Literal["success", "failure", "unsupported"]
    message: str
    detail: Optional[str] = None


def _test_out(result: TestResult) -> CredentialTestOut:
    """The wire form of one test: its four fields by name, so the forge's raw answer a `TestResult`
    carries for `git_access` (`TestResult.http`) never reaches a response."""
    return CredentialTestOut(ok=result.ok, status=result.status, message=result.message, detail=result.detail)


# The test body's own shape: no `name`/`description`, plus `id`, which the CRUD body rejects.
_TEST_BODY_FIELDS = frozenset({"type", "config", "id"})


# The one credential route with no `own_credential`, deliberately. It makes the CP send an
# authenticated request to an operator-typed host, and `app/credentials/testers.py` has no SSRF or
# private-address guard (self-hosted hosts have no fixed set). `credentials:edit` keeps that power
# explicitly delegated; widening this route needs the guard written first.
@router.post("/api/credentials/test", response_model=CredentialTestOut)
@feature("credentials", "edit")
def test_credential(
    raw_body: Any = Body(default=None),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CredentialTestOut:
    """Test the connection described by `{type, config, id?}` — on demand, one bounded exchange.

    A completed test is always a 200, whatever its outcome; only a malformed request is a 4xx (422 only
    for non-JSON). Wrong-typed body values are a value-free 400 (property 2).

    `id` is a LOOKUP when the body carries no sealed envelope — resolved exactly as PATCH resolves it
    (`_scoped_row_or_404`: unknown and ungranted are one 404) — and an AAD INPUT first, a lookup second,
    when it does: the create form mints its id and tests before saving, so a miss fills no stored
    secret and never 404s. The browser sends `id` only for a saved row or with a sealed envelope.

    With a row, blank secrets are filled from it, pinned to the row's own host and audited
    (`_with_stored_secrets`, the load-bearing control). A sealed envelope is refused when it is one the
    table already stores (`_refuse_replayed_stored_envelopes`), audited like a fill, and opened only
    when a tester exists (`_opened_inline_secrets`; an untestable type costs no private operation).

    Outbound calls are bounded by `follow_redirects=False`, one shared time budget
    (`credentials/testers.py::_request`), and the `credentials:edit` key. The CP dialing an
    operator-supplied host is accepted. `generic_api_key` appends no path, so Test is a status-code
    oracle over the whole URL; no response body reaches the operator. An inline-config test with no `id`
    leaves no audit row.
    """
    raw = _parsed_test_body(raw_body)
    spec = _validated_type(raw.get("type"))
    # Refused for every tenant org (`tenancy/ids.py::is_tenant_org`), before the config is read: a
    # tenant superadmin could grant `credentials:edit` and aim the CP's egress at internal hosts. The
    # operator's own staff keep Test. Restoring it for tenants needs the egress guard.
    if is_tenant_org(user.org_id):
        # The tenant's sentence, in their console's words (`tenancy/copy.py`).
        raise HTTPException(
            status_code=403,
            detail=operator_hosted("Test", "The credential is stored and used without a test."),
        )
    if _posts_a_nodevault_envelope(spec, raw.get("config")):
        # D12/AC-5.9: a NodeVault-sealed value (the create form tests before saving) is never admitted here.
        return _test_out(nodevault_untestable_result())
    config = _validated_test_config(spec, raw.get("config"))
    credential_id = _optional_uuid(raw.get("id"), "id must be a credential uuid")
    posted_sealed = {key: value for key, value in config.items() if isinstance(value, SealedSecret)}
    if posted_sealed and credential_id is None:
        raise HTTPException(status_code=400, detail="id is required to test a sealed secret")
    # `_EDIT_KEY` on both arms: this route has no `own_credential`.
    row: Optional[Credential] = None
    # Only this arm's row is stamped with the result (`credential_health.record_test`): an envelope's or an
    # inline config's test is of values not saved, whatever row its id names. Accepted (architecture A5): this
    # arm tests posted non-secret fields and retyped plaintext secrets as typed, so Test on a dirty edit form
    # stamps the form's result on the row.
    saved_id: Optional[UUID] = None
    if credential_id is not None and not posted_sealed:
        # Nothing sealed: the id names a saved row, so it is a lookup — 404 for unknown and ungranted
        # alike, as PATCH answers (tenancy: the two must be indistinguishable).
        row = _scoped_row_or_404(session, principal, credential_id, _EDIT_KEY)
        saved_id = row.id
    elif credential_id is not None:
        # An envelope: the id is its AAD first (the create form mints it and tests before saving, so
        # it names no row yet) and a lookup second — a miss fills no stored secret and never 404s.
        row = session.scalar(
            scoped_credential_select_for(principal, _EDIT_KEY).where(Credential.id == credential_id)
        )
    if row is not None and row.nodevault:
        # D12/AC-5.9: the test runs here, and this process can open no NodeVault secret. Before any open.
        return _test_out(nodevault_untestable_result())
    # A bad request is an error even for an untestable type, so `{"id": "7", "type": "xero"}` is a 400.
    tester = TESTERS.get(spec.slug)
    if posted_sealed:
        # Before any open, whatever `row` resolved to: a lifted envelope names a row the caller may
        # not see, so a row-based check would be the hole.
        _refuse_replayed_stored_envelopes(session, posted_sealed)
        if tester is not None:
            # Audited like a fill, first (a hung request still leaves a trace); never opened for an
            # untestable type, which would spend a private operation to send nothing.
            _audit_test(
                session, user, spec, credential_id, row.name if row is not None else None,
                _posted_origin(spec, config),
            )
            config = _opened_inline_secrets(spec, credential_id, config)
    if row is not None:
        config, withheld = _with_stored_secrets(session, row, spec, config, user, principal)
        if withheld is not None:
            return withheld

    if tester is None:
        # The reason the types endpoint serves, Connect remedy included, so pressing Test never drops it.
        result = unsupported_result(spec.slug)
        reason = _untestable_reason_out(spec, connect_served=_connect_out(spec.slug) is not None)
        return _test_out(result._replace(message=reason) if reason is not None else result)
    # One time budget for all outbound calls, created after DB work so latency is not charged to it.
    deadline = new_deadline()
    try:
        result = tester(config, deadline)
    except Exception as exc:  # noqa: BLE001 - see below
        # Unreachable by design (testers map transport failures); guards against uvicorn logging the
        # row. `from None` keeps the original off the response.
        raise HTTPException(
            status_code=500, detail=f"connection test failed ({type(exc).__name__})"
        ) from None
    if saved_id is not None:
        credential_health.record_test(session, saved_id, result.ok)
    return _test_out(result)


def _parsed_test_body(raw: Any) -> dict[str, Any]:
    """The test body as a plain dict, with every failure mode a value-free 400.

    Not `_parsed_body` (the CRUD contract rejects `id`); same discipline.
    """
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    extras = sorted(set(raw) - _TEST_BODY_FIELDS)
    if extras:
        raise HTTPException(status_code=400, detail=f"unknown field '{extras[0]}'")
    return raw


def _posts_a_nodevault_envelope(spec: CredentialTypeSpec, value: Any) -> bool:
    """Whether a test body's config carries a secret envelope naming a NodeVault key (`k = "nv:…"`). Read
    off `k` alone, before `_validated_test_config` (which admits KMS envelopes only)."""
    if not isinstance(value, dict):
        return False
    return any(
        isinstance(value.get(key), dict) and is_nodevault_key_id(value[key].get("k"))
        for key in secret_keys(spec.slug)
    )


def _validated_test_config(spec: CredentialTypeSpec, value: Any) -> dict[str, Any]:
    """The posted config for a test: storage-side hazards checked, tester-side ones left alone.

    NUL bytes and the length cap are checked as on write (the value is about to be saved). The URL
    scheme is left to the tester, whose 200 `failure` is more actionable. An undeclared key is rejected,
    matching `_merged_config`, rather than misreported as a missing field.
    """
    posted = _validated_posted_config(value)
    declared = {field.key for field in spec.fields}
    config: dict[str, Any] = {}
    for key in sorted(posted):
        if key not in declared:
            raise HTTPException(
                status_code=400,
                detail=f"unknown field '{key}' for credential type '{spec.slug}'",
            )
        submitted = posted[key]
        # An explicit `null` means "not supplied", exactly like a blank string.
        if submitted is None:
            continue
        if isinstance(submitted, dict) and key in secret_keys(spec.slug):
            # A browser-sealed value typed into the form; opened under the body's `id` by the caller.
            try:
                config[key] = sealed_secret_from_wire(submitted, field=key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            continue
        if not isinstance(submitted, str):
            raise HTTPException(status_code=400, detail=f"{key} must be a string")
        if len(submitted) > _VALUE_MAX_LEN:
            raise HTTPException(
                status_code=400, detail=f"{key} must be at most {_VALUE_MAX_LEN} characters"
            )
        if "\x00" in submitted:
            raise HTTPException(status_code=400, detail=f"{key} must not contain a NUL byte")
        config[key] = submitted
    return config


def _opened_inline_secrets(
    spec: CredentialTypeSpec, credential_id: UUID, config: dict[str, Any]
) -> dict[str, Any]:
    """`config` with each browser-sealed secret opened for the test — under `credential_id`, the field
    and the destination the posted config itself spells, exactly what the browser sealed under.

    This is a consumption seam (the value is about to be sent to the vendor), hence the one place on the
    Test route that performs the private operation on an inline value. A 400 names the field only.
    """
    opened = dict(config)
    destination = sealing_destination(spec.slug, config)
    for key, value in config.items():
        if not isinstance(value, SealedSecret):
            continue
        try:
            opened[key] = open_sealed(
                value, credential_id=credential_id, field=key, destination=destination
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"{key} could not be opened: it was sealed for another credential, field or URL",
            ) from None
        # `WrappingKeyUnavailable` (an outage at the key, not a bad envelope) propagates to the app-wide
        # 503 handler (`main.py`), the one answer every consumer route gives.
    return opened


def _posted_origin(spec: CredentialTypeSpec, config: dict[str, Any]) -> Optional[tuple[str, str, int]]:
    """The origin the posted config would dial: the host field is read from the spec, through
    `_effective_base_url`, so a fixed-host type and an operator-host type answer alike."""
    key = f.key if (f := _operator_host_field(spec)) else "base_url"
    return request_origin(_effective_base_url(spec, config.get(key)))


def _with_stored_secrets(
    session: Session,
    row: Credential,
    spec: CredentialTypeSpec,
    config: dict[str, Any],
    user: AdminUser,
    principal: Principal | None,
) -> tuple[dict[str, Any], Optional[CredentialTestOut]]:
    """`config` with each blank secret filled from the stored row, or a result to return instead of
    testing.

    The stored secret is pinned to the row's own host: filling it for a request-supplied `base_url`
    would make Test a read primitive for every token. A differing scheme/host/port fills nothing and
    the operator must retype. Every fill is audited first (`_audit_test`). Not covered: PATCH the host,
    then test (both need `credentials:edit` and a grant). Nothing is filled across a type change (keys
    overlap between types). Only secret keys the current type declares are filled.
    """
    # `row` is the caller's editable row for the body's `id` (resolved by the route); a type mismatch
    # fills nothing (keys overlap between types).
    if row.type != spec.slug:
        return config, None

    stored = dict(row.config or {})
    declared = {field.key for field in spec.fields}
    fillable = {
        key
        for key in secret_keys(spec.slug, stored) & declared
        if is_blank(config.get(key)) and not is_blank(stored.get(key))
    }
    if not fillable:
        return config, None

    # Both sides go through `_effective_base_url`. A fill needs both origins non-`None` and equal.
    host = _operator_host_field(spec)
    key = host.key if host else "base_url"
    # What the withheld sentence calls the host: the field the operator edits, else the fixed default.
    host_label = host.label if host else "base URL"
    posted_origin = _posted_origin(spec, config)
    stored_origin = request_origin(_effective_base_url(spec, stored.get(key)))
    if posted_origin is None or stored_origin is None:
        # No comparable origin on a side: fill nothing. Its own branch because `None == None` once
        # matched an unparseable posted URL to a stored `http://` row.
        return config, None
    if not _same_origin(posted_origin, stored_origin):
        # The host moved, so the secret stays; named per field. Only when a tester exists: an
        # untestable type makes no request.
        if spec.slug not in TESTERS:
            return config, None
        label = next(field.label for field in spec.fields if field.key in fillable)
        return config, _test_out(withheld_secret_result(label, host_label))

    # Audited only when a tester exists; an untestable type sends nothing.
    if spec.slug in TESTERS:
        _audit_test(session, user, spec, row.id, row.name, posted_origin)
    # Opened here, where the value is consumed by the tester; one private operation per filled field.
    filled = dict(config)
    for key in fillable:
        try:
            filled[key] = open_secret(row, key)
        except ValueError:
            label = next(field.label for field in spec.fields if field.key == key)
            return config, _test_out(withheld_secret_result(label, host_label))
        # A `WrappingKeyUnavailable` propagates to the app-wide 503 handler (`main.py`).
    return filled, None


def _audit_test(
    session: Session,
    user: AdminUser,
    spec: CredentialTypeSpec,
    credential_id: UUID,
    name: Optional[str],
    origin: Optional[tuple[str, str, int]],
) -> None:
    """One audit row per test that OPENS a secret — a stored one filled, or a sealed one posted inline
    — written before the request goes out. `name` is None for an inline open under an id no row holds
    yet (the create form).

    Nothing else records it (`UserActivityMiddleware` records GETs only; the access log has method and
    path). Written first so a hung request still leaves a trace. Names the host, never `config`. Tests
    that open nothing are not audited.

    Over-records: the tester may refuse after the row is written, and `github_app`'s recognition call
    never sends the `client_secret` it fills. A per-tester table of transmitted secrets could drift from
    the code, so over-recording is the cheaper error.
    """
    host = origin[1] if origin is not None else "(no base URL)"
    label = f"credential {name}" if name is not None else "unsaved credential"
    with _scrubbed_write(session, "audit the connection test for"):
        record_write(
            session,
            user,
            "credential_tested",
            f"Tested {spec.label} {label} against {host}",
            data={"credential_id": credential_id, "type": spec.slug, "host": host},
        )
        session.commit()
