"""Credential write routes — create, update, folder move and delete."""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import delete as sql_delete
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.actors import stamp_actor
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.object_access import stamp_owner_on_create, validated_owner_user
from app.authz.principal import Principal, current_principal
from app.authz.catalog import permission_key
from app.authz.visibility import has_project_key
from app.db import get_session
from app import edition_features, nodevault_policy
from app.operator_settings import reads as settings_reads
from app.credential_crypto import SealedSecret
from app import git_probe
from app.git_access import GIT_CREDENTIAL_TYPES
from app.models.credentials import (
    Credential,
    CredentialIdTombstone,
)
from app.models.identity import AdminUser
from app.tenancy.labels import org_refs
from app.tenancy.seam import bound_org
from app.routes.credentials.project_pool import _require_pool_manager
from app.routes.credentials.roster import _uuid_array
from app.routes.credentials.guards import (
    _DELETE_KEY,
    _EDIT_KEY,
    _may_edit,
    _not_in_plan,
    _org_of,
    _refuse_a_broken_nodevault_invariant,
    _refuse_ineligible_nodevault,
    _refuse_kept_nodevault_secrets,
    _refuse_kept_secrets_if_destination_moved,
    _refuse_turning_nodevault_on,
    _scoped_row_or_404,
)
from app.routes.credentials.credential_wire import (
    CredentialOut,
    _SealScope,
    _actor_names,
    _audit,
    _credential_out,
    _credential_ownership,
    _merged_config,
    _optional_uuid,
    _parsed_body,
    _require_declared_fields,
    _scrubbed_write,
    _validated_description,
    _home_labels,
    _refuse_duplicates_in_home,
    _validated_home_project,
    _validated_name,
    _validated_nodevault,
    _validated_posted_config,
    _validated_type,
)

# The community edition has no Telegram: the one read is behind the same check (`update_credential`).
if edition_features.enabled("telegram"):
    from app import telegram_bot

router = APIRouter(tags=["credentials"])


# Log an outcome plus an id, never a code, verifier, secret, token or config.
log = logging.getLogger(__name__)


@router.post("/api/credentials", response_model=CredentialOut, status_code=201)
@feature("credentials", "create", own_credential=True)
def create_credential(
    raw_body: Any = Body(default=None),
    user: AdminUser = Depends(require_user),
    # For the owner stamp below (`owned_credential_clause`): a caller with no org role owns its row.
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CredentialOut:
    body = _parsed_body(raw_body)
    spec = _validated_type(body.type)
    name = _validated_name(body.name)
    description = _validated_description(body.description)
    nodevault = _validated_nodevault(body.nodevault) if body.nodevault is not None else False
    posted = _validated_posted_config(body.config)
    client_id = _optional_uuid(body.id, "id must be a uuid")
    if client_id is not None and _id_is_minted(session, client_id):
        # A 409, not a 500 out of the unique violation: the client re-mints and retries. First, so no later
        # check answers about a row the caller may not see.
        raise HTTPException(status_code=409, detail="a credential with this id already exists")
    if nodevault:
        # A new NodeVault credential: refused while the plan is off or frozen (D11), for an OAuth2 type, or
        # where the control plane would use it. A transient row (never added) carries what the rules read.
        _refuse_turning_nodevault_on(
            session, bound_org(), spec,
            Credential(type=spec.slug, org_id=bound_org(), created_by_id=user.id), posted,
        )
    # The org the row will be stamped into at flush (`tenancy/write_seam.py`), whose NodeVault keys an
    # `nv:` envelope may name; an unbound create names none, so its NodeVault envelopes are refused.
    seal = _SealScope(nodevault=nodevault, session=session, org_id=bound_org())
    config = _merged_config(spec, {}, posted, reset=True, seal=seal)
    _require_declared_fields(spec, config)
    _refuse_a_broken_nodevault_invariant(config, nodevault)
    ownership = _credential_ownership(body.ownership, principal)
    owner_user_id = validated_owner_user(
        session, principal, _optional_uuid(body.owner_user_id, "owner_user_id must be a uuid"),
        ownership=ownership, org_id=user.org_id,
    )
    credential_id = client_id
    if credential_id is None:
        if any(isinstance(value, SealedSecret) for value in config.values()):
            # A browser envelope is bound to the id the browser sealed under; without it nothing could
            # ever open the secret.
            raise HTTPException(
                status_code=400, detail="id is required when config carries sealed secrets"
            )
        credential_id = uuid4()

    row = Credential(
        id=credential_id, name=name, type=spec.slug, description=description or "", config=config,
        nodevault=nodevault,
    )
    stamp_actor(row, user, created=True)
    # Personal or org row, decided before the INSERT so `owner_id` is written with it.
    stamp_owner_on_create(principal, row, ownership, owner_user_id=owner_user_id)
    row.home_project_id = _validated_home_project(
        session, principal, body.home_project_id, org_id=bound_org(),
        org_row=row.owner_id is None and not row.system,
    )
    _require_homing(principal, row, leaving=None, joining=row.home_project_id)
    _refuse_duplicates_in_home(session, [(row, name, row.home_project_id)])
    # The flush emits the config-bearing INSERT, so it is inside the guard. No console event
    # (property 3).
    with _scrubbed_write(session, "create"):
        session.add(row)
        session.flush()
        _audit(session, user, "credential_created", f"Created {spec.label} credential {name}", row,
               owner_user_id=row.owner_id)
        if nodevault:
            _audit(session, user, "credential_nodevault_toggled",
                   f"Turned NodeVault on for {spec.label} credential {name}", row)
        session.commit()
        session.refresh(row)
    return _credential_out(
        row,
        actor_names=_actor_names(session, [row]),
        org_labels=org_refs(session, [row.org_id]),
        home_labels=_home_labels(session, [row]),
        editable=_may_edit(principal, row),
    )


@router.patch("/api/credentials/{credential_id}", response_model=CredentialOut)
@feature("credentials", "edit", own_credential=True)
def update_credential(
    credential_id: UUID,
    raw_body: Any = Body(default=None),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CredentialOut:
    """Partial update. A field absent from the body is untouched, including each key inside `config`
    (per-key merge, `_merged_config`), since a swap would drop the secret the form cannot send back.

    Everything is validated before anything is assigned, so no autoflush persists half an update."""
    body = _parsed_body(raw_body)
    if "id" in body.model_fields_set:
        raise HTTPException(status_code=400, detail="id is not editable")
    if body.model_fields_set & {"ownership", "owner_user_id"}:
        # Personal or org is decided once, at create: flipping a live row would hand a colleague's
        # personal secret to the org, or take an org row away from every project that uses it.
        raise HTTPException(status_code=400, detail="ownership is not editable")
    row = _scoped_row_or_404(session, principal, credential_id, _EDIT_KEY)
    submitted = body.model_fields_set

    # The type in force for this request decides declared and secret keys, so resolve it first.
    type_changed = "type" in submitted and body.type != row.type
    spec = _validated_type(body.type) if "type" in submitted else _validated_type(row.type)
    name = _validated_name(body.name) if "name" in submitted else row.name
    # Against the stored name NORMALIZED: a full-body PATCH resending a legacy uppercase name changes nothing
    # the uniqueness rule reads, so a pre-existing duplicate stays editable (it is stored normalized).
    name_changed = "name" in submitted and name != _validated_name(row.name)
    description = (
        _validated_description(body.description) if "description" in submitted else row.description
    )
    nodevault = (
        _validated_nodevault(body.nodevault)
        if "nodevault" in submitted and body.nodevault is not None
        else bool(row.nodevault)
    )
    home_project_id = (
        _validated_home_project(
            session, principal, body.home_project_id, org_id=row.org_id,
            org_row=row.owner_id is None and not row.system,
        )
        if "home_project_id" in submitted
        else row.home_project_id
    )
    stored = dict(row.config or {})
    posted = _validated_posted_config(body.config)
    toggled = nodevault != bool(row.nodevault)
    if nodevault and toggled:
        _refuse_turning_nodevault_on(session, row.org_id, spec, row, {**stored, **posted})
    elif nodevault:
        if type_changed:
            # Re-typed, its config starts empty: a new NodeVault credential in all but id, frozen like one (D11).
            org = _org_of(session, row.org_id)
            if org is None or not nodevault_policy.status(session, org).allowed:
                raise _not_in_plan()
        # Still NodeVault: the final type and config must stay eligible (a re-type, an added Slack app token).
        _refuse_ineligible_nodevault(session, spec, row, {**stored, **posted})
    seal = _SealScope(nodevault=nodevault, session=session, org_id=row.org_id)
    config = _merged_config(spec, stored, posted, reset=type_changed, seal=seal)
    token_replaced = posted.get("token") is not None and spec.slug in GIT_CREDENTIAL_TYPES
    if token_replaced and "grant_dead_at" not in posted:
        # A new token for a git credential its forge had reported revoked is a reconnect (D-A14): the
        # recorded revocation goes with the old token, and the next use checks the new one at once.
        config.pop("grant_dead_at", None)
    _require_declared_fields(spec, config)
    _refuse_kept_secrets_if_destination_moved(row, spec, stored, config)
    if toggled and not nodevault:
        _refuse_kept_nodevault_secrets(config)
    # Over the MERGED config: a kept KMS secret beside a posted NodeVault one (or the reverse) is refused.
    _refuse_a_broken_nodevault_invariant(config, nodevault)
    _require_homing(principal, row, leaving=row.home_project_id, joining=home_project_id)
    if name_changed or home_project_id != row.home_project_id:
        # Asked before anything is assigned, about the home the row WILL have.
        _refuse_duplicates_in_home(session, [(row, name, home_project_id)])

    row.name = name
    row.home_project_id = home_project_id
    row.description = description or ""
    row.type = spec.slug
    # Reassigned: the setter seals (`app/models/credentials.py::Credential.config`); a mutation would seal nothing.
    row.config = config
    row.nodevault = nodevault
    if token_replaced:
        git_probe.forget(row.id)
    # Without `created=True`: authorship survives edits (`app/actors.py::stamp_actor`).
    stamp_actor(row, user)
    with _scrubbed_write(session, "update"):
        _audit(session, user, "credential_updated", f"Updated {spec.label} credential {name}", row,
               owner_user_id=row.owner_id)
        if toggled:
            _audit(session, user, "credential_nodevault_toggled",
                   f"Turned NodeVault {'on' if nodevault else 'off'} for {spec.label} credential {name}", row)
        session.commit()
        session.refresh(row)
    if edition_features.enabled("telegram") and spec.slug == telegram_bot.CREDENTIAL_TYPE and posted.get("bot_token"):
        _reregister_designated_bot(row.id)
    return _credential_out(
        row,
        actor_names=_actor_names(session, [row]),
        org_labels=org_refs(session, [row.org_id]),
        home_labels=_home_labels(session, [row]),
        editable=_may_edit(principal, row),
    )


_PROJECTS_EDIT_KEY = permission_key("projects", "edit")


def _require_homing(
    principal: Principal | None, row: Credential, *, leaving: Optional[int], joining: Optional[int]
) -> None:
    """Homing a credential in a project is attaching it there, and leaving a home project detaching it, so
    each passes the pool's own checks (`project_pool.py`): `projects:edit` on that project (the attach/detach
    routes' guard) and `_require_pool_manager` (attaching for the new home, detaching for the old). Nothing
    when the home does not change."""
    if joining == leaving:
        return
    for project_id, attaching in ((joining, True), (leaving, False)):
        if project_id is None:
            continue
        if not has_project_key(principal, _PROJECTS_EDIT_KEY, project_id):
            raise HTTPException(
                status_code=403,
                detail=f"{_PROJECTS_EDIT_KEY} on the project is required to "
                + ("home a credential in it" if attaching else "move a credential out of it"),
            )
        _require_pool_manager(principal, row, attaching=attaching)


def _reregister_designated_bot(credential_id: UUID) -> None:
    """A new token on the designated Telegram bot credential is a new bot: point it at this deployment's
    webhook, as picking it in Settings does (`routes/settings.py::put_settings`), or it links nobody. After the
    commit and best effort: the token is saved either way, and Settings > Integrations > Telegram shows the
    webhook's state and re-registers it."""
    telegram_bot.forget_token()
    if settings_reads.get_text(telegram_bot.SETTING_KEY) != str(credential_id):
        return
    try:
        telegram_bot.register_webhook(str(credential_id))
    except telegram_bot.TelegramError as exc:
        log.warning("telegram: re-registering the webhook after a token change failed (%s)", exc)
    except Exception as exc:
        # The token is already saved: an unexpected fault (opening the credential) must not 500 the PATCH.
        log.warning("telegram: re-registering the webhook after a token change failed (%s)", type(exc).__name__)


# ── Folder names ──────────────────────────────────────────────────────────────
# A folder is a `/`-separated `name` prefix, not an entity (`apps/web/src/lib/nameTree.ts`), so moving one
# RENAMES every credential beneath it — the bulk move (`bulk.py`, `POST /api/credentials/bulk/move-folder`), in
# ONE transaction through `rename_credentials` below. Segment normalization mirrors the browser's `nameSegments`.

_PATH_SEPARATOR = "/"


def _name_segments(name: str) -> list[str]:
    """`a / b//c` → `['a', 'b', 'c']` — the browser's `nameSegments`, so both sides agree on
    which folder a name sits in."""
    return [segment.strip() for segment in name.split(_PATH_SEPARATOR) if segment.strip()]


def rename_credentials(
    session: Session, user: AdminUser, rows: Sequence[Credential], renamed: Mapping[UUID, str]
) -> None:
    """Rename each of `rows` (locked and edit-checked by the caller) to its validated `renamed` name, and commit:
    the one write behind the folder move and the bulk move (`bulk.py`). Per home, against every row of it, and
    among the moved rows themselves — two names differing only in case would otherwise land on one — in one
    query for the whole set, so a collision refuses all of it."""
    _refuse_duplicates_in_home(session, [(row, renamed[row.id], row.home_project_id) for row in rows])
    with _scrubbed_write(session, "move"):
        for row in rows:
            old_name = row.name
            row.name = renamed[row.id]
            stamp_actor(row, user)
            _audit(session, user, "credential_updated", f"Moved credential {old_name} to {row.name}", row)
        session.commit()


@router.delete("/api/credentials/{credential_id}", status_code=204)
@feature("credentials", "delete", own_credential=True)
def delete_credential(
    credential_id: UUID,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    row = _scoped_row_or_404(session, principal, credential_id, _DELETE_KEY)
    # The community edition has no Jaah-managed NodeLink storage.
    if edition_features.enabled("nodelink"):
        from app import nodelinks

        nodelinks.refuse_managed_credential(session, row.id)
    delete_credentials(session, user, [row])
    return Response(status_code=204)


def delete_credentials(session: Session, user: AdminUser, rows: Sequence[Credential]) -> None:
    """Delete `rows` (found under the caller's delete scope, managed rows refused) and commit: the one delete
    behind the single-row route and the bulk one (`bulk.py`). A fixed number of statements whatever the count:
    the audit rows and tombstones flush as batched inserts, the rows go in one `DELETE … = ANY(:ids)`."""
    ids = [row.id for row in rows]
    with _scrubbed_write(session, "delete"):
        for row in rows:
            # Names the owner: an Org Owner/Admin may delete a member's personal row
            # (`authz.credential_write_clause`); `record_write` adds `acting_as`.
            _audit(session, user, "credential_deleted", f"Deleted credential {row.name}", row,
                   owner_user_id=row.owner_id)
        # The id is retired with the row, in the same transaction (`models/credentials.py::CredentialIdTombstone`):
        # re-minted, a copy of the row's envelopes would open again.
        session.add_all([CredentialIdTombstone(id=credential_id) for credential_id in ids])
        # DML passes no read seam (`test_tenancy_bypass_sites.py`): the bound org is fenced here, as
        # `write_seam._refuse_cross_org_deletes` fences an ORM delete.
        fence = [Credential.org_id == bound_org()] if bound_org() is not None else []
        deleted = session.execute(
            sql_delete(Credential).where(Credential.id == _uuid_array(ids), *fence)
            .execution_options(synchronize_session=False)
        ).rowcount
        if deleted != len(ids):
            # Unreachable while the caller's rows are locked under its scope; never tombstone a row left alive.
            session.rollback()
            raise HTTPException(status_code=409, detail="the credentials changed — reload and try again")
        for row in rows:
            session.expunge(row)
        session.commit()


def _id_is_minted(session: Session, credential_id: UUID) -> bool:
    """Is the client's `id` already a credential's — live in ANY org, or a deleted row's tombstone?

    UNSCOPED BY DESIGN (`test_tenancy_bypass_sites.py`, `exact-value-probe`): the id is an AAD input
    the client minted, and the property guarded is of the whole table. Scoped, a row in another org
    would MISS and the INSERT's unique violation would be a 500; a tombstone is never org-scoped
    (`models/credentials.py::CredentialIdTombstone`). Answers a bool from a value the caller sent; a v4 id is not
    guessable, so the answer is no oracle."""
    return session.scalar(
        text(
            "SELECT 1 FROM credentials WHERE id = :id "
            "UNION ALL SELECT 1 FROM credential_id_tombstones WHERE id = :id LIMIT 1"
        ),
        {"id": credential_id},
    ) is not None
