"""The credentials wire shape — schemas, body validators, the one secret-omitting serializer and the write/audit helpers."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence, TypeVar
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Text, Uuid, and_, any_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import edition_features
from app.actors import actor_names as resolve_actor_names
from app.authz.object_access import (
    CREDENTIAL_OWNERSHIP_CHOICES,
    OWNERSHIP_ORG,
    OWNERSHIP_PERSONAL,
    OWNERSHIP_SYSTEM,
    validated_ownership,
)
from app.authz.catalog import PROJECTS_VIEW_KEY
from app.authz.principal import Principal, creates_org_rows
from app.authz.visibility import scoped_project_select
from app.config import get_settings
from app.entity_shape import is_blank, iso_or_none, optional_uuid
from app.credentials.registry import (
    CREDENTIAL_TYPES,
    FieldSpec,
    CredentialTypeSpec,
    TYPE_FEATURES,
    host_field,
    secret_keys,
    type_label,
)
from app.credential_crypto import (
    NodeVaultInvariantError,
    SealedSecret,
    nodevault_key_hash,
    sealed_secret_from_wire,
)
from app.git_access import rest_username_problem
from app.credentials.oauth_wizards import connect_for_type
from app.credentials.testers import (
    _allowed_host_clause,
    _split_base_url,
    write_time_problem,
)
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.projects import Project
from app.tenancy.labels import OrgRef, org_ref_of
from app.tenancy.seam import bound_org
from app.user_activity import record_write
from app.routes.credentials.guards import _nodevault_refusal


_NAME_MAX_LEN = 200
_DESCRIPTION_MAX_LEN = 2000
_VALUE_MAX_LEN = 4096


# ── Wire models ───────────────────────────────────────────────────────────────

class FieldSpecOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    kind: str
    required: bool
    placeholder: Optional[str] = None
    # Pre-filled on a NEW row when the type is chosen (`registry.FieldSpec.default`); non-secret only.
    default: Optional[str] = None
    # Helper text under the input (`registry.FieldSpec.help`).
    help: Optional[str] = None


class OAuthConnectOut(BaseModel):
    """The control-plane OAuth2 flow a type can be filled by, or absent when it has none.

    Display facts only: no client, secret or endpoints (the start route 303s to the authorize URL).
    `provider_label` is null when the vendor is not knowable in advance (generic OAuth2); the form must
    not invent one, and the start route resolves it from the row's authorize URL host.
    """

    slug: str
    action_label: str
    provider_label: Optional[str] = None
    # The URL the operator must register with the vendor as a redirect URI, served so it is not guessed
    # (a mismatch surfaces as `exchange_failed` after consent). A deployment fact, served here because
    # this block renders the panel. Not a secret: the callback refuses anything without a signed cookie.
    redirect_uri: str


class SealKeyOut(BaseModel):
    """The deployment's secret-wrapping PUBLIC key, for the browser to seal secrets under before they
    leave the page (`apps/web/src/lib/credentialSeal.ts`). Public by construction; `key_id` is
    what every envelope stamps, so a key change makes the browser's next seal name the new key."""

    key_id: str
    algorithm: str
    # Base64 DER SubjectPublicKeyInfo — WebCrypto's `spki` import format.
    public_key: str


class CredentialTypeOut(BaseModel):
    slug: str
    label: str
    testable: bool
    # The two inputs of the sealing destination the browser must mirror (`registry.sealing_destination`):
    # the key of the operator's host field (None for a fixed-host type) and the type's default host.
    host_field: Optional[str] = None
    default_base_url: Optional[str] = None
    # Why this type has no connection test, from the registry (`testers.py` pins the pairing); shown
    # before Test is pressed, and reaching the banner via `unsupported_result` after.
    untestable_reason: Optional[str] = None
    fields: list[FieldSpecOut]
    # Absent for types with no control-plane OAuth2 flow. One nullable object, so the form's fields cannot
    # disagree.
    oauth_connect: Optional[OAuthConnectOut] = None
    # The registry's `require_one_of` key groups (empty for most types): the form renders these OPTIONAL
    # fields outright, since a row saves only once one group is wholly filled.
    require_one_of: list[list[str]] = []


class CredentialOut(BaseModel):
    id: UUID
    name: str
    type: str
    type_label: str
    # Never null since revision 0132: an explicit `null` PATCH clears it to `''`.
    description: str = ""
    # Resolved display names, never ids (`app/actors.py::actor_names`); `None` for a deleted actor.
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    # Who the row belongs to, by name: a personal row's owner (`owner_id`, possibly a member an Org
    # Owner/Admin made it for), else its creator — `roster.ROSTER_OWNER`, the Owner filter's rule. `None` when
    # that user does not resolve.
    owner: Optional[str] = None
    # The org pair: `org_id` verbatim and `org` the resolved label (`app/tenancy/labels.py::OrgRef`).
    # `(set, null)` is an id naming no `orgs` row (no FK, revision 0181), rendered as the id. `org_id` is
    # NOT NULL since revision 0200, so `(null, null)` cannot occur; the `Optional`s are kept for the wire
    # contract. The `?org_id=none` arm (`_ORG_ID_NONE`) is dead but kept pending a product decision
    # (`tests/test_credentials_roster.py::test_org_id_none_stays_vocabulary_and_now_selects_no_row`).
    org_id: Optional[UUID]
    org: Optional[OrgRef]
    # Secret keys are absent from this dict, not masked and not nulled. See `_credential_out`.
    config: dict[str, Any]
    # Which secret keys hold a value, so the edit form says "leave blank to keep" only where true.
    secrets_set: list[str]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Whether this caller's PATCH on this row would be admitted, so a client can withhold Edit. Served
    # on every row-carrying payload (roster, by-id read, POST/PATCH/wizard echoes). `None` means "not
    # asked", never "read-only" (`CredentialEditPage.tsx` switches on `false`); no route serves `None`.
    # `_may_edit` is a pure predicate over a loaded row and a `Principal`, so per-row cost is trivial, and
    # it restates PATCH's admission rule (`authz.credential_write_clause`), which differs from GET's.
    editable: Optional[bool] = None
    # Who the row belongs to: its creator alone (`personal`), the org (`org`), or the control plane
    # (`system`, 0325 — never a runner's). Derived from `owner_id` and `system`.
    ownership: Literal["personal", "org", "system"]
    # NodeVault: whether this row's secrets are sealed under the org's NodeVault key, and that
    # key's hash — the envelopes' own `k`, derived (`credential_crypto.nodevault_key_hash`), never stored
    # twice. None for a KMS row.
    nodevault: bool = False
    nodevault_key_hash: Optional[str] = None
    # The project home (`Credential`'s NAMING): its id and name, both null unless the row lives in a project.
    home_project_id: Optional[int] = None
    home_project: Optional[str] = None
    # The locked prefix the name sits under, outermost first (`_home_path`): `[org]`, `[org, 'system']`,
    # `[org, <project>]` or `[org, <owner email>]`. Never part of `name`.
    home_path: list[str] = []
    # Health, the stored facts as `Credential` documents them: whether a secret is held, when the
    # credential stops working, its last use, and its last connection test's time and verdict (null: none yet).
    has_stored_secret: bool = True
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    last_tested_at: Optional[str] = None
    last_test_ok: Optional[bool] = None


@dataclass(frozen=True)
class _HomeLabels:
    """What `_home_path` names beyond the org label, resolved once per page (`_home_labels`): home project
    names by id and personal owners' emails by id."""

    projects: Mapping[int, str]
    emails: Mapping[UUID, str]


class CredentialListOut(BaseModel):
    items: list[CredentialOut]
    # Needed to disable "Next" honestly. Exact on the OFFSET path; on the keyset path at most
    # `roster.ROSTER_COUNT_CAP`, with `total_capped` set when more match ("10,000+").
    total: int
    total_capped: bool = False
    # The keyset path's next page (`?cursor=`), null on the last page and on the OFFSET path.
    next_cursor: Optional[str] = None
    page: int
    per: int


class CredentialIn(BaseModel):
    """The create and update body. Every field is `Any` and extras are allowed (property 2): pydantic's
    `extra_forbidden` error would echo the rejected value. Extras are rejected in `_parsed_body`, which
    names the key alone.
    """

    model_config = ConfigDict(extra="allow")

    name: Any = None
    type: Any = None
    description: Any = None
    config: Any = None
    # Create only: the browser mints the row's UUID before sealing, since every envelope's AAD carries it
    # (`app/credential_crypto.py`). Refused on PATCH.
    id: Any = None
    # Create only: `personal`, `org` or `system` (`authz.validated_ownership`); absent is `personal` for
    # everyone. Only an Org Owner/Admin or an operator may ask for `org` or `system`. Refused on PATCH.
    ownership: Any = None
    # Create only, with `personal`: the org member the row is made for (`authz.validated_owner_user`,
    # D6); absent is the caller. Naming someone else needs an Org Owner/Admin or an operator. Refused on PATCH.
    owner_user_id: Any = None
    # Whether the row's secrets are sealed under the org's NodeVault key (a boolean; absent keeps the row's
    # own on PATCH, `false` on create). Decides which key a posted envelope may name (`_SealScope`).
    nodevault: Any = None
    # The project the row lives in (`Credential`'s NAMING), create and PATCH; `null` clears it to org shared.
    # Only an org row may have one, and only a project of its org the caller sees (`_validated_home_project`).
    home_project_id: Any = None


_DECLARED_BODY_FIELDS = frozenset(
    {"name", "type", "description", "config", "id", "ownership", "owner_user_id", "nodevault", "home_project_id"}
)


_Body = TypeVar("_Body", bound=BaseModel)


def _parsed_body(
    raw: Any, model: type[_Body] = CredentialIn, declared: frozenset[str] = _DECLARED_BODY_FIELDS  # type: ignore[assignment]
) -> _Body:
    """The request body as a `model`, with every failure mode turned into a value-free 400: a non-object
    is a 400, an unknown key is a 400 naming the key, and nothing pydantic validates a value.

    A non-object body is rejected here (pydantic would echo it). `model_validate` on a dict cannot fail.
    Parameterised for the wizard bodies so no copy can forget the extras check.
    """
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    body = model.model_validate(raw)
    extras = sorted(set(body.model_extra or ()) - declared)
    if extras:
        raise HTTPException(status_code=400, detail=f"unknown field '{extras[0]}'")
    return body


# ── Validation + config merge ─────────────────────────────────────────────────

def _validated_name(value: Any) -> str:
    """The NORMALIZED name (`Credential`'s NAMING: lowercase, `/`-segmented, each segment trimmed, empty
    segments dropped), or a 400 naming the field; never echoes the value. Nothing else about its format is
    refused: anything the user types is accepted, normalized."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="name is required")
    # NUL-checked: Postgres cannot store one, and a failed statement logs its bound parameters.
    if "\x00" in value:
        raise HTTPException(status_code=400, detail="name must not contain a NUL byte")
    cleaned = "/".join(segment.strip() for segment in value.lower().split("/") if segment.strip())
    if not cleaned:
        raise HTTPException(status_code=400, detail="name is required")
    if len(cleaned) > _NAME_MAX_LEN:
        raise HTTPException(
            status_code=400, detail=f"name must be at most {_NAME_MAX_LEN} characters"
        )
    return cleaned


class DuplicateName(HTTPException):
    """`_refuse_duplicates_in_home`'s 409, carrying the colliding `name` for the bulk refusal (`bulk.py`)."""

    def __init__(self, name: str) -> None:
        super().__init__(status_code=409, detail=f"a credential named {name} already exists in this scope")
        self.name = name


def _refuse_duplicates_in_home(
    session: Session, landing: Sequence[tuple[Credential, str, Optional[int]]], *, org_id: Optional[UUID] = None,
) -> None:
    """409 when a `(row, name, home_project_id)` in `landing` would share its HOME (the row's org, `system` and
    `owner_id`, and `home_project_id`) and `name` case-insensitively with another row of that home, or with
    another entry of `landing` — the one per-home uniqueness check, asked by create, PATCH and folder move
    before anything is assigned. ONE query against every row of those homes, not the caller's readable
    roster: an ORM select, so the tenancy seam bounds it, plus each row's own org once stamped. The rows of
    `landing` themselves are excluded (a folder move's renamed names can never meet its old ones: a move into
    the folder's own parent is refused). Write-time only: a duplicate that predates the rule stays readable.

    Race-free: a transaction-scoped advisory lock per `org|home|lower(name)` is taken first (sorted, so two
    writers never deadlock) and held to the commit, so two concurrent writes of one name in one home serialize
    and the second sees the first's row.

    `org_id` names the org every row of `landing` will be IN, for a change of org (`bulk.py`); None, its own."""

    def org_of(row: Credential) -> Optional[UUID]:
        if org_id is not None:
            return org_id
        # A create's row is not stamped yet, and a create is always bound to the org it will be stamped into
        # (`tenancy/write_seam.py`); an existing row names its own, which an operator's unbounded plane needs.
        return row.org_id if row.org_id is not None else bound_org()

    keys: list[tuple[Optional[UUID], bool, Optional[UUID], Optional[int], str]] = []
    for row, name, home_project_id in landing:
        key = (org_of(row), bool(row.system), row.owner_id, home_project_id, name.lower())
        if key in keys:
            raise DuplicateName(name)
        keys.append(key)
    # One statement whatever the set's size: `unnest` yields the sorted array in order, each row taking its lock.
    locks = sorted("|".join(str(part) for part in key) for key in keys)
    lock_names = func.unnest(literal(locks, type_=ARRAY(Text()))).table_valued("name")
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_names.c.name))).select_from(lock_names))
    homes = []
    for home_org, system, owner_id, home_project_id, lowered in keys:
        clauses = [
            Credential.system.is_(system),
            Credential.owner_id.is_(None) if owner_id is None else Credential.owner_id == owner_id,
            Credential.home_project_id.is_(None)
            if home_project_id is None
            else Credential.home_project_id == home_project_id,
            func.lower(Credential.name) == lowered,
        ]
        if home_org is not None:
            clauses.append(Credential.org_id == home_org)
        homes.append(and_(*clauses))
    stmt = select(
        Credential.org_id, Credential.system, Credential.owner_id, Credential.home_project_id,
        func.lower(Credential.name),
    ).where(or_(*homes))
    own_ids = [row.id for row, _, _ in landing if row.id is not None]
    if own_ids:
        stmt = stmt.where(~(Credential.id == any_(literal(own_ids, type_=ARRAY(Uuid())))))
    taken = session.execute(stmt).all()
    for key, (_, name, _) in zip(keys, landing, strict=True):
        home_org, system, owner_id, home_project_id, lowered = key
        if any(
            (home_org is None or hit[0] == home_org) and tuple(hit[1:]) == (system, owner_id, home_project_id, lowered)
            for hit in taken
        ):
            raise DuplicateName(name)


def _validated_home_project(
    session: Session, principal: Principal | None, value: Any, *, org_id: Optional[UUID], org_row: bool
) -> Optional[int]:
    """`CredentialIn.home_project_id`: `None` clears it; otherwise the id of a project in `org_id` the caller
    can see (`projects:view`, `scoped_project_select`), on an org row only (`Credential`'s NAMING). 400 for a
    malformed value, a personal/system row or no `org_id` (an unbound create), 404 for a project that is not there or not visible to them."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=400, detail="home_project_id must be an integer")
    if not org_row:
        raise HTTPException(
            status_code=400, detail="only an org credential can live in a project"
        )
    if org_id is None:
        # An unbound create (the operator plane): with no org the filter below would be skipped.
        raise HTTPException(status_code=400, detail="pick the org before a home project")
    stmt = scoped_project_select(principal, key=PROJECTS_VIEW_KEY).where(
        Project.id == value, Project.org_id == org_id
    )
    if session.scalar(stmt.with_only_columns(Project.id)) is None:
        raise HTTPException(status_code=404, detail="no such project")
    return value


def _validated_description(value: Any) -> Optional[str]:
    """`None` clears it; anything else must be a bounded string (one oversized write would cost the
    single-worker API process its memory, `worker_guard.py::assert_single_api_worker`)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="description must be a string")
    if "\x00" in value:
        raise HTTPException(status_code=400, detail="description must not contain a NUL byte")
    if len(value) > _DESCRIPTION_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"description must be at most {_DESCRIPTION_MAX_LEN} characters",
        )
    return value


def in_edition(slug: str) -> bool:
    """Whether this edition offers credential type `slug`: no feature claims it (`registry.TYPE_FEATURES`), or the
    one that does is enabled."""
    feature = TYPE_FEATURES.get(slug)
    return feature is None or edition_features.enabled(feature)


def _validated_type(value: Any) -> CredentialTypeSpec:
    """The registry spec for `value`, or a 400. The whole of `type` validation (no DB enum or CHECK).

    The one 400 naming a submitted value: a type slug is never a credential. Pinned by the contract. A
    type of a feature this edition lacks is unknown, as its type list omits it (`in_edition`)."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="type is required")
    spec = CREDENTIAL_TYPES.get(value)
    if spec is None or not in_edition(value):
        raise _no_such_credential_type(value)
    return spec


def _no_such_credential_type(value: str) -> HTTPException:
    """'There is no such type'. Truncated, so the echo is bounded."""
    return HTTPException(status_code=400, detail=f"unknown credential type '{value[:64]}'")


@dataclass(frozen=True)
class _SealScope:
    """Which wrapping keys a posted envelope may name on this write: the deployment's KMS key, or — for a
    NodeVault credential — the org's `current`/`previous` NodeVault key, looked up in `session` for
    `org_id` (`credential_crypto.sealed_secret_from_wire`). Absent = KMS only."""

    nodevault: bool
    session: Any
    org_id: Optional[UUID]


def _validated_nodevault(value: Any) -> bool:
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail="nodevault must be true or false")
    return value


def _validated_value(
    spec: CredentialTypeSpec, field: FieldSpec, value: Any, seal: Optional[_SealScope] = None
) -> str | SealedSecret:
    """One config value, checked for the hazards that are storage-side rather than tester-side.

    Every kind is a string — except that a SECRET may arrive as a sealed envelope the browser produced
    (returned as a `SealedSecret`, stored as-is; the console never posts a plaintext secret) — so any
    other dict/list/number is malformed. A `url` field must carry an http(s)
    scheme (not the SSRF check; stops `javascript:` links). `spec.require_https_base_url` is enforced
    here too via `_split_base_url` (catches `HTTP://`), rejecting an unparseable value on flagged types
    only. NUL bytes are rejected (unstorable in `jsonb`; the failed INSERT would log parameters).
    `write_time_problem` applies the type's per-field rules from `testers.py`, so an untestable value
    cannot be saved.
    """
    if field.kind == "secret" and isinstance(value, dict):
        # A browser-sealed envelope (`credentialSeal.ts`): shape and bounds are checked, the wrapping key
        # must be this deployment's, and the value is never opened here — nothing on the CRUD path may.
        try:
            if seal is None:
                return sealed_secret_from_wire(value, field=field.key)
            return sealed_secret_from_wire(
                value, field=field.key, nodevault=seal.nodevault, session=seal.session, org_id=seal.org_id
            )
        except NodeVaultInvariantError as exc:
            raise _nodevault_refusal(exc) from None
        except ValueError as exc:
            # The message names the field and a shape fact, never a value (`credential_crypto`).
            raise HTTPException(status_code=400, detail=str(exc)) from None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field.key} must be a string")
    if seal is not None and seal.nodevault and field.kind == "secret" and value.strip():
        # A NodeVault secret is sealed in the browser; a plaintext one would be KMS-sealed by this process.
        raise HTTPException(
            status_code=422,
            detail=f"{field.key} must be sealed under the organization's NodeVault key, not sent in the clear",
        )
    if len(value) > _VALUE_MAX_LEN:
        raise HTTPException(
            status_code=400, detail=f"{field.key} must be at most {_VALUE_MAX_LEN} characters"
        )
    if "\x00" in value:
        raise HTTPException(status_code=400, detail=f"{field.key} must not contain a NUL byte")
    problem = write_time_problem(spec, field, value)
    if problem is not None:
        # The tester's own wording, opening with `field.key` so `CredentialEditPage.tsx` places it by the
        # field. It echoes a value only for a reserved header name, one of six known literals
        # (`_header_name_clause`).
        raise HTTPException(status_code=400, detail=problem)
    if field.kind == "url":
        # Stored as validated, not as typed: the value checked must be the value kept.
        cleaned = value.strip()
        if cleaned and not cleaned.lower().startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"{field.key} must be an http(s) URL")
        if cleaned and spec.require_https_base_url:
            base, _host = _split_base_url(cleaned)
            if base is None or not base.startswith("https://"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{field.key} must use https:// - {spec.label} credentials must never be "
                        "saved against a plaintext base URL."
                    ),
                )
        if field is host_field(spec):
            clause = _allowed_host_clause(spec, cleaned)
            if clause is not None:
                raise HTTPException(status_code=400, detail=f"{field.key} {clause}")
        return cleaned
    return value


def _merged_config(
    spec: CredentialTypeSpec,
    stored: dict[str, Any],
    posted: dict[str, Any],
    *,
    reset: bool,
    seal: Optional[_SealScope] = None,
) -> dict[str, Any]:
    """`stored` updated by `posted`, per key — never a whole-object swap.

      - a secret key posted as `""` (or absent) keeps the stored value ("leave blank to keep").
      - any key posted as explicit `null` is deleted, never stored as JSON `null`.
      - anything else replaces.

    Setting an undeclared key is rejected (dropping would erase a retired field's value on an unrelated
    PATCH); deleting one is allowed. `reset=True` starts empty, on create and on a type change, so
    `base_url` is not carried to a different type.
    """
    declared = {field.key: field for field in spec.fields}
    for key in sorted(posted):
        # An explicit `null` on an undeclared key deletes it, so a retired field's secret can always be
        # removed. Setting one stays rejected: unclassified, so nothing can say serving it is safe.
        if key not in declared and posted[key] is not None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown field '{key}' for credential type '{spec.slug}'",
            )
    secrets = secret_keys(spec.slug, stored)
    merged: dict[str, Any] = {} if reset else dict(stored)
    for key, value in posted.items():
        if value is None:
            merged.pop(key, None)
            continue
        checked = _validated_value(spec, declared[key], value, seal)
        if key in secrets and isinstance(checked, str) and not checked.strip():
            continue
        merged[key] = checked
    return merged


def _require_one_of_choices(spec: CredentialTypeSpec) -> str:
    """`API token, or Client ID and Client secret` — built from the spec's labels, no per-type copy."""
    labels = {field.key: field.label for field in spec.fields}
    return ", or ".join(" and ".join(labels[key] for key in group) for group in spec.require_one_of)


def _require_declared_fields(spec: CredentialTypeSpec, config: dict[str, Any]) -> None:
    """400 on the first required field that is absent or blank, then when no `require_one_of` group is
    wholly filled (a stored sealed secret counts as filled), then on a REST username the token's kind
    needs (`git_access.rest_username_problem`). Names the KEY only, except the `require_one_of` refusal,
    which names the field LABELS because it reaches an operator who sees labels, never keys."""
    for field in spec.fields:
        if field.required and is_blank(config.get(field.key)):
            raise HTTPException(status_code=400, detail=f"{field.key} is required")
    if spec.require_one_of and not any(
        all(not is_blank(config.get(key)) for key in group) for group in spec.require_one_of
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Fill {_require_one_of_choices(spec)}.",
        )
    missing = rest_username_problem(spec.slug, config)
    if missing is not None:
        raise HTTPException(
            status_code=400,
            detail=f"{missing} is required for a {spec.label} API token: enter the Atlassian account email it belongs to",
        )


@contextmanager
def _scrubbed_write(session: Session, action: str, noun: str = "credential"):
    """Wrap every statement of a write, converting any DB failure into a 500 that carries nothing from
    the row.

    `SQLAlchemyError.__str__` appends `[parameters: ...]`, and the whole config is one JSONB parameter.
    Not chained, never logged; the class name is kept. Spans the whole write (the flush emits the
    INSERT). The rollback is guarded (its `__context__` would be the original). `UnicodeError` is caught
    too: the one DB failure SQLAlchemy does not wrap."""
    try:
        yield
    except (SQLAlchemyError, UnicodeError) as exc:
        try:
            session.rollback()
        except (SQLAlchemyError, UnicodeError):
            pass
        raise HTTPException(
            status_code=500,
            detail=f"could not {action} {noun} ({type(exc).__name__})",
        ) from None


# ── Serialization ─────────────────────────────────────────────────────────────


def _host_field_key(spec: CredentialTypeSpec) -> Optional[str]:
    """The key of the operator's host field (`registry.host_field`), or None for a fixed-host type."""
    field = host_field(spec)
    return field.key if field is not None else None


def _untestable_reason_out(spec, *, connect_served: bool) -> Optional[str]:
    """`spec.untestable_reason`, plus its Connect remedy only for a reader who is offered Connect.

    The copy is the registry's (`credentials/registry.py`); this decides only whether the reader has the
    control it names, per `_connect_out`."""
    if spec.untestable_reason is None:
        return None
    if not connect_served or not spec.untestable_connect_remedy:
        return spec.untestable_reason
    return f"{spec.untestable_reason} {spec.untestable_connect_remedy}"


def _connect_out(type_slug: str) -> Optional[OAuthConnectOut]:
    """The control-plane connect block for `type_slug`, or None when that type has no provider.

    Served to every org: a provider that pins no endpoints reads them off the row, and
    `_refuse_a_caller_supplied_endpoint_without_the_edit_key` holds that row's Token URL to the one
    destination rule. The start route resolves the row through the scoped select, so a caller only ever
    connects a row they can edit.
    """
    provider = connect_for_type(type_slug)
    if provider is None:
        return None
    return OAuthConnectOut(
        slug=provider.slug,
        action_label=provider.action_label,
        provider_label=provider.provider_label,
        # The same function both legs call, so the advised URL is the one the exchange is bound to.
        redirect_uri=_connect_redirect_uri(),
    )


def _credential_out(
    row: Credential,
    *,
    actor_names: Optional[dict[UUID, str]] = None,
    org_labels: Mapping[UUID, OrgRef],
    home_labels: _HomeLabels,
    editable: Optional[bool] = None,
) -> CredentialOut:
    """The one place a credential row becomes a response, and therefore the one place secrets are
    stripped. Every route returns through here; nothing else may serialize this model.

    `org_labels` is keyword-only with no default (`routes/portfolio.py`'s rule), resolved once per page
    (`app/tenancy/labels.py`), and so is `home_labels` (`_home_labels`). Both `org_id` and `org` come from
    `row.org_id`, so `(null, set)` is unrepresentable. `secret_keys` is asked with the row's own config, never the slug, so it fails closed
    on a renamed field or a retired type.
    """
    config = dict(row.config or {})
    secrets = secret_keys(row.type, config)
    spec = CREDENTIAL_TYPES.get(row.type)
    # Declared secrets first in registry order, then unclassifiable ones sorted.
    declared_order = [field.key for field in spec.fields] if spec is not None else []
    secret_order = declared_order + sorted(set(config) - set(declared_order))
    return CredentialOut(
        id=row.id,
        name=row.name,
        type=row.type,
        type_label=type_label(row.type),
        description=row.description,
        config={
            key: value
            for key, value in config.items()
            if key not in secrets and not isinstance(value, SealedSecret)
        },
        secrets_set=[
            key for key in secret_order if key in secrets and not is_blank(config.get(key))
        ],
        created_at=iso_or_none(row.created_at),
        updated_at=iso_or_none(row.updated_at),
        # `.get`, so a deleted actor renders absent; no map gives `None`.
        created_by=(actor_names or {}).get(row.created_by_id),
        updated_by=(actor_names or {}).get(row.updated_by_id),
        owner=(actor_names or {}).get(_owner_id_of(row)),
        # Both from `row.org_id`; see the pair's note on `CredentialOut`.
        org_id=row.org_id,
        org=org_ref_of(org_labels, row.org_id),
        editable=editable,
        ownership=_ownership_of(row),
        nodevault=bool(row.nodevault),
        nodevault_key_hash=nodevault_key_hash(config),
        home_project_id=row.home_project_id,
        home_project=(
            home_labels.projects.get(row.home_project_id) if row.home_project_id is not None else None
        ),
        home_path=_home_path(row, org_labels=org_labels, home_labels=home_labels),
        has_stored_secret=row.has_stored_secret,
        expires_at=iso_or_none(row.expires_at),
        last_used_at=iso_or_none(row.last_used_at),
        last_tested_at=iso_or_none(row.last_tested_at),
        last_test_ok=row.last_test_ok,
    )


def _home_path(
    row: Credential, *, org_labels: Mapping[UUID, OrgRef], home_labels: _HomeLabels
) -> list[str]:
    """`CredentialOut.home_path` — the ONE place a row's home becomes its locked prefix: the org label (its
    name, or the id when no `orgs` row resolves), then `system`, the home project's name or the personal
    owner's email; nothing more for an org shared row. An unresolved project or email renders as its id."""
    org = org_ref_of(org_labels, row.org_id)
    path = [org.name if org is not None else str(row.org_id)]
    if row.system:
        path.append(OWNERSHIP_SYSTEM)
    elif row.home_project_id is not None:
        path.append(home_labels.projects.get(row.home_project_id, str(row.home_project_id)))
    elif row.owner_id is not None:
        path.append(home_labels.emails.get(row.owner_id, str(row.owner_id)))
    return path


def _home_labels(session: Session, rows: Sequence[Credential]) -> _HomeLabels:
    """Every name `_home_path` resolves for `rows` — home project names and personal owners' emails — in one
    query each, never per row."""
    project_ids = {row.home_project_id for row in rows if row.home_project_id is not None}
    owner_ids = {row.owner_id for row in rows if row.owner_id is not None}
    projects = dict(
        session.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids))).tuples().all()
    ) if project_ids else {}
    emails = dict(
        session.execute(select(AdminUser.id, AdminUser.email).where(AdminUser.id.in_(owner_ids))).tuples().all()
    ) if owner_ids else {}
    return _HomeLabels(projects=projects, emails=emails)


def _owner_id_of(row: Credential) -> Optional[UUID]:
    """`roster.ROSTER_OWNER` for one loaded row: whom `CredentialOut.owner` names."""
    return row.owner_id if row.owner_id is not None else row.created_by_id


def _actor_names(session: Session, rows: Sequence[Credential]) -> dict[UUID, str]:
    """Every name `_credential_out` resolves for `rows` — creator, updater and owner — in one query."""
    return resolve_actor_names(
        session, [i for row in rows for i in (row.created_by_id, row.updated_by_id, row.owner_id)]
    )


def _ownership_of(row: Credential) -> str:
    """`CredentialOut.ownership`, from the two columns that decide it."""
    if row.owner_id is not None:
        return OWNERSHIP_PERSONAL
    return OWNERSHIP_SYSTEM if row.system else OWNERSHIP_ORG


def _credential_ownership(value: Any, principal: Principal | None) -> str:
    """A credential create's `ownership`: the posted choice, or `personal` when absent — for everyone, an
    Org Owner/Admin included, so sharing a secret with the org is always a choice someone made."""
    validated = validated_ownership(
        value, may_create_org=creates_org_rows(principal), choices=CREDENTIAL_OWNERSHIP_CHOICES
    )
    return OWNERSHIP_PERSONAL if validated is None else validated


def _optional_uuid(value: Any, detail: str) -> Optional[UUID]:
    """An optional uuid-string field as a uuid, or None when absent; a 400 carrying `detail` otherwise.

    The type check precedes the parse: `UUID()` raises `AttributeError`/`TypeError` on a non-string,
    which a `ValueError` catch would let escape as a 500. Never echoed (property 2)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=detail)
    parsed = optional_uuid(value)
    if parsed is None:
        raise HTTPException(status_code=400, detail=detail)
    return parsed


def _validated_posted_config(value: Any) -> dict[str, Any]:
    """The submitted `config` as a dict. `null` or absent means "no config keys", a no-op on PATCH; a
    wipe is an explicit `null` per key."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="config must be an object")
    return value


def _audit(
    session: Session, user: AdminUser, kind: str, summary: str, row: Credential,
    *, owner_user_id: Optional[UUID] = None, extra: Optional[dict[str, Any]] = None,
) -> None:
    """One write-audit row per mutation. The payload carries no `config`: identity makes the mutation
    reviewable, and the values are the credential. A create made for another member names them
    (`owner_user_id`); a change of org names both orgs (`extra`); `record_write` adds `acting_as` for an
    operator acting as the org.

    `record_write` stages on the caller's transaction, so an unwritable audit rolls the mutation back."""
    data: dict[str, Any] = {"credential_id": row.id, "type": row.type, "name": row.name}
    if owner_user_id is not None:
        data["owner_user_id"] = owner_user_id
    data.update(extra or {})
    record_write(session, user, kind, summary, data=data)


def _connect_redirect_uri() -> str:
    """The callback URL, absolute — a binding on the exchange, not just a destination.

    Built from `frontend_url`, never the request (as `git_connect._redirect_uri`): it must be byte
    identical on start and exchange, and proxy headers vary. The operator must register this exact URL.
    """
    return f"{get_settings().frontend_url.rstrip('/')}/api/credentials/connect/callback"
