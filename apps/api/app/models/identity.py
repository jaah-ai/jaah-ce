"""People: the identity a person is, the admin user they sign in as, their sign-in factors and lockout."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    event,
    ForeignKey,
    func,
    Identity,
    Index,
    insert,
    Integer,
    String,
    Text,
    select,
    text,
    UniqueConstraint,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship
from sqlalchemy.orm.attributes import instance_dict
from sqlalchemy.schema import FetchedValue
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.base import Base, EntityMixin, org_unique, OrgScoped, timestamp_column, UuidPkMixin


#: The person-level columns: one home on `identities` (`UserIdentity`), read off a membership row through
#: `AdminUser`'s read-only hybrids and written by `set_identity_fields`. The `admin_users` columns of the same
#: names are unmapped (`migrations/env.py::_RETIRING_COLUMNS`) until the revision-2 contraction drops them.
IDENTITY_FIELDS: tuple[str, ...] = (
    "email",
    "email_verified_at",
    "password_hash",
    "first_name",
    "last_name",
    "google_sub",
    "github_user_id",
    "microsoft_tenant_id",
    "microsoft_object_id",
)

#: `ix_identities_name_trgm`'s expression: a person's "First Last", lowercased (revision 202610240400). Spelled as
#: Postgres reports it back (casts and grouping explicit), so alembic's comparison (`tests/test_schema_drift.py`) agrees.
_NAME_TRGM = (
    "lower((coalesce(first_name, ''::character varying)::text || ' '::text)"
    " || coalesce(last_name, ''::character varying)::text)"
)


class UserIdentity(EntityMixin, Base):
    """ONE PERSON: what signs in, whichever org they act as (multi-org membership).

    No `name`: a person is labelled from `first_name` + `last_name` (`app/identities.py::person_label`),
    never from a third, hand-edited column.

    An `admin_users` row is one membership of one identity in one org; every org-owned fact (role
    bindings, groups, Slack link, notification settings) stays on that row, so isolation keeps deriving
    through `admin_users` exactly as before. Platform plane, never org-filtered
    (`app/tenancy/manifest.py`): read to resolve the caller, never listed by an org-scoped route.

    `session_epoch` is the identity epoch: sign-out and a password change bump it and retire every
    membership's cookie at once, while disabling ONE membership bumps only that row's own epoch
    (`app/auth.py::session_row_is_live`). `last_active_admin_id` is the membership the next sign-in opens
    first (`app/login_funnel.py::pick_membership`).

    A row is made by a membership's insert (find-or-create by email, `_attach_identity`), never by hand: a
    membership is created as an `AdminUser` and its insert attaches or creates its identity."""

    __tablename__ = "identities"
    # Unmaps `EntityMixin.name`; see the class docstring.
    name = None  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "microsoft_tenant_id", "microsoft_object_id", name="uq_identities_microsoft_identity"
        ),
        CheckConstraint(
            "(microsoft_tenant_id IS NULL) = (microsoft_object_id IS NULL)",
            name="ck_identities_microsoft_identity_pair",
        ),
        # The credential roster's operator search by owner email (revision 202610232130).
        Index(
            "ix_identities_email_trgm", text("lower(email)"),
            postgresql_using="gin", postgresql_ops={"lower(email)": "gin_trgm_ops"},
        ),
        # The Owner filter's search by "First Last" (revision 202610240400); the expression is
        # `routes/credentials/reads.py::_OWNER_NAME`'s.
        Index(
            "ix_identities_name_trgm", text(_NAME_TRGM),
            postgresql_using="gin", postgresql_ops={_NAME_TRGM: "gin_trgm_ops"},
        ),
    )

    # The actor columns fall back to the system actor (SET DEFAULT): a person outlives the membership that
    # made or last edited them, and an org purge must not be blocked by one (`test_org_delete_order.py`).
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET DEFAULT", name="fk_identities_created_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )
    updated_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET DEFAULT", name="fk_identities_updated_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # When a Google, Microsoft or GitHub sign-in last proved `email`; NULL since the last change.
    email_verified_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # The provider identities, linked lazily on a first sign-in (`google_sub`'s rule, `routes/auth.py`).
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    github_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    microsoft_tenant_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    microsoft_object_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # Display preferences only (`app/preferences.py::IDENTITY_PREFERENCE_KEYS`); the rest are per org.
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    session_epoch: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # A pointer the identity's own sign-in reads, never an org source (`test_tenancy_composite_keys.py`).
    last_active_admin_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", use_alter=True,
                   name="fk_identities_last_active_admin_id"),
        nullable=True,
    )


class IdentityFieldIsReadOnly(AttributeError):
    """A person-level field was assigned on a membership row. Write it with `set_identity_fields`."""


def _identity_hybrid(field: str) -> hybrid_property:
    """`AdminUser.<field>`: the identity's value, read-only. One builder for all nine fields.

    Instance: the loaded identity's value, or — while none is loaded (pending before its insert attaches one,
    or detached before it was read) — the value the row was made with (`AdminUser._pending_identity`).
    Class: a correlated scalar subquery, so `select(...).where(AdminUser.email == ...)` still compiles (the email census in
    `tests/test_identity_email_census.py` says where such a comparison is allowed). Setter: refuses."""

    def fget(self):
        identity = self._loaded_identity()
        return getattr(identity, field) if identity is not None else self._pending_fields().get(field)

    def fset(self, value):
        raise IdentityFieldIsReadOnly(
            f"AdminUser.{field} is the identity's (`identities.{field}`); write it with "
            "app.models.identity.set_identity_fields"
        )

    def expr(cls):
        return (
            select(getattr(UserIdentity, field))
            .where(UserIdentity.id == cls.identity_id)
            .correlate_except(UserIdentity)
            .scalar_subquery()
            .label(field)
        )

    return hybrid_property(fget, fset, expr=expr)


class AdminUser(EntityMixin, OrgScoped, Base):
    """ONE MEMBERSHIP: a person (`UserIdentity`) in one org — the row a session acts as.

    Authorization is `is_superadmin` (a full bypass) plus group memberships; `role` is retained but
    NON-AUTHORITATIVE. The person-level fields (`IDENTITY_FIELDS`) read through to `identities` and are
    written only by `set_identity_fields`; everything else here is this org's. `password_hash` is nullable
    so a Google-only invited user can exist without one, authenticating via `/api/auth/google`.

    A row is never `is_superadmin` when an invitation accept or a self-create made it
    (`assert_member_row_not_superadmin`), so operator power exists only on an operator-plane row.

    No `name`: every label a human reads for a person — Created by, Assigned to, Answered by, an
    @mention, a picker row — derives from `first_name` + `last_name`, else the email
    (`app/identities.py::person_label`; the batch form is `app/actors.py::actor_names`)."""

    __tablename__ = "admin_users"
    # Unmaps `EntityMixin.name`; see the class docstring.
    name = None  # type: ignore[assignment]
    __table_args__ = (
        org_unique(__tablename__),
        # One membership per person per org.
        UniqueConstraint("identity_id", "org_id", name="uq_admin_users_identity_id_org_id"),
        # At most one system actor.
        Index("uq_admin_users_one_system", "is_system", unique=True, postgresql_where=text("is_system")),
        Index(
            "uq_admin_users_slack_link_code_hash",
            "slack_link_code_hash",
            unique=True,
            postgresql_where=text("slack_link_code_hash IS NOT NULL"),
        ),
        # One membership per IdP account in an org (`app/sso_signin.py::bind_subject`).
        Index(
            "uq_admin_users_sso_subject",
            "org_id", "sso_protocol", "sso_issuer", "sso_subject",
            unique=True,
            postgresql_where=text("sso_subject IS NOT NULL"),
        ),
    )
    # RETURNING brings the server defaults back on the flush that inserts the row.
    __mapper_args__ = {"eager_defaults": True}

    # The person (revision 0360). Nullable until the revision-2 contraction: this release names it on every
    # insert (`_attach_identity`), and the outgoing release's inserts still reach revision 0360's BEFORE
    # INSERT trigger, which find-or-creates it from the unmapped legacy columns.
    identity_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        # `use_alter`: identities' own keys point back at admin_users (actors, last active membership).
        ForeignKey("identities.id", ondelete="RESTRICT", name="fk_admin_users_identity_id", use_alter=True),
        nullable=True,
        server_default=FetchedValue(),
    )
    # The one relationship in this file: the hybrids below read through it on every membership row, and
    # `selectin` loads it in one batch per statement (no N+1, and no outer join under `FOR UPDATE`).
    identity: Mapped[Optional[UserIdentity]] = relationship(
        UserIdentity, foreign_keys=lambda: [AdminUser.identity_id], lazy="selectin"
    )

    email = _identity_hybrid("email")
    # When a Google, Microsoft or GitHub sign-in last proved `email` (the provider verified it), cleared
    # whenever the email changes. Email notifications go only to a proven address
    # (`notify_targets.email_routes`); NULL means never proven since the last change.
    email_verified_at = _identity_hybrid("email_verified_at")
    password_hash = _identity_hybrid("password_hash")
    first_name = _identity_hybrid("first_name")
    last_name = _identity_hybrid("last_name")
    # The provider identities, linked lazily on a first successful sign-in for a pre-existing account
    # (never auto-created). GitHub: the stable numeric id, never the login. Microsoft: the (`tid`, `oid`)
    # pair, both-or-neither.
    google_sub = _identity_hybrid("google_sub")
    github_user_id = _identity_hybrid("github_user_id")
    microsoft_tenant_id = _identity_hybrid("microsoft_tenant_id")
    microsoft_object_id = _identity_hybrid("microsoft_object_id")

    is_superadmin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The operator's override of this membership's password door: NULL follows the role rule
    # (`app/auth.py::password_refusal` — shut for a superadmin, open otherwise), true opens it, false shuts it.
    password_login: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # The operator's second-factor exemption for this membership: set, a sign-in skips the code
    # (`app/mfa.py::skip_reason`) and is audited `mfa_skipped`.
    mfa_exempt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The operator's client-error reporting switch for this membership (revision 202610312100): set, the console
    # reports its render errors, chunk failures, stalls and uncaught errors (`app/client_events.py`).
    client_telemetry: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="admin", server_default="admin"
    )
    activated_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # This org's preferences; the display keys live on the identity (`app/preferences.py`).
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    # The membership epoch: disable/enable of THIS row bumps it. Sign-out and a password change bump the
    # identity's (`UserIdentity.session_epoch`).
    session_epoch: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_seen_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True, index=True)
    # The newest one-time code this user DMs the Slack bot to connect their Slack (`app/slack_link.py`): a
    # SHA-256 of the code, never the code, and when it stops working. Redeem clears the hash and keeps the
    # expiry, which is how `slack_link.link_state` tells redeemed from expired.
    slack_link_code_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_link_code_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The retiring pre-0291 Git Access pointer: selections live in `UserGitConnection` (`app/git_connections.py`),
    # and this is read only as the grandfather of an org credential selected before the org-role rule
    # (`authz.git_identity_select`'s `current_id`). Never written, except that `git_connections.delete_connection`
    # clears it when it names a deleted connection (the outgoing release still falls back to it).
    # SET NULL fails users closed. `use_alter` breaks the FK cycle with `credentials.created_by_id`.
    git_credential_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credentials.id", ondelete="SET NULL", use_alter=True,
                   name="fk_admin_users_git_credential_id"),
        nullable=True,
    )
    # The system actor: exactly one row (partial unique index where true), fixed id
    # `app/entity_shape.py::SYSTEM_ACTOR_ID`. NON-LOGINABLE — `app/routes/auth.py` refuses it before the
    # credential check with the same generic failure, so no error oracles the row's existence.
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Disabled (G-02): set, the row signs in by no method and holds no live session
    # (`app/auth.py::session_row_is_live`); NULL is enabled. The row and every attribution to it stay.
    # Written by `routes/users.py::disable_user`/`enable_user`, which also bump `session_epoch`, and by an
    # invited membership's making and accept (`invitation_id`, `app/org_invitations.py`).
    disabled_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Who disabled it; cleared on re-enable. SET NULL: deleting that operator must not block the history.
    disabled_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_admin_users_disabled_by_id"),
        nullable=True,
    )
    # The org invitation this membership is waiting on: set, the row was made when an admin
    # invited a person who already has an account elsewhere, and it stays DISABLED — signs in by no method,
    # holds no principal, is sent no notification — until they accept (`app/org_invitations.py`), which
    # clears both. Admins see it on People as invited (or declined) and grant it access meanwhile.
    # `use_alter`: `org_invitations` points back here through its provenance columns.
    invitation_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("org_invitations.id", ondelete="SET NULL", name="fk_admin_users_invitation_id", use_alter=True),
        nullable=True,
    )
    # The identity provider's own id for this membership (`externalId`), as its SCIM client last sent it
    # (`app/scim.py`); NULL for a member SCIM never touched. The IdP's handle, never ours: never unique, never trusted.
    scim_external_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The SAML/OIDC account this membership is bound to: `saml`/`oidc`, the IdP's entity id or issuer, and
    # its NameID or `sub`, stored on the first SSO sign-in; every later one must assert the same subject
    # (`app/sso_signin.py::bind_subject`). NULL until a member first signs in by SSO.
    sso_protocol: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    sso_issuer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sso_subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __init__(self, **kwargs: Any) -> None:
        # The person-level fields wait on the instance until the insert attaches the row to the identity of
        # that email, or creates one from them (`_attach_identity`).
        pending = {field: kwargs.pop(field) for field in IDENTITY_FIELDS if field in kwargs}
        super().__init__(**kwargs)
        if pending:
            self._pending_identity = pending

    def _pending_fields(self) -> dict[str, Any]:
        """The person-level values this row was made with (or `set_identity_fields` gave it before an identity
        was loaded): what the insert makes the identity from, and the read while none is loaded."""
        return self.__dict__.get("_pending_identity") or {}

    def _loaded_identity(self) -> Optional[UserIdentity]:
        """The identity, when it can be read without a detached load; None while the row has none yet."""
        state = instance_dict(self)
        if state.get("identity") is not None:
            return state["identity"]
        if object_session(self) is None:
            return None
        # Attached: `identity_id` reloads an expired row; None only while pending (before its insert).
        return self.identity if self.identity_id is not None else None


def set_identity_fields(user: AdminUser, **fields: Any) -> None:
    """THE ONE WRITE PATH for a person-level field: the identity's column, or — on a row not inserted yet —
    the values its insert makes the identity from. Refuses a name outside `IDENTITY_FIELDS`."""
    unknown = set(fields) - set(IDENTITY_FIELDS)
    if unknown:
        raise ValueError(f"not identity fields: {sorted(unknown)}")
    identity = user._loaded_identity()
    if identity is None:
        user._pending_identity = {**user._pending_fields(), **fields}
        return
    for field, value in fields.items():
        setattr(identity, field, value)


#: Revision 0360's refusal, raised by the database so a caller sees the same `IntegrityError` either release raises.
_EMAIL_HELD = (
    "DO $$ BEGIN RAISE EXCEPTION 'an account already holds this email' "
    "USING ERRCODE = 'unique_violation', CONSTRAINT = 'admin_users_email_key'; END $$"
)


@event.listens_for(AdminUser, "before_insert")
def _attach_identity(_mapper, connection, user: AdminUser) -> None:
    """Every membership insert names its person: the row's own `identity_id`/`identity` when the caller gave
    one, else the identity of its email (case-insensitive, oldest first) — an orphaned one (no membership
    left) is adopted and takes the new row's values, a live one refuses the insert exactly as revision 0360's
    trigger does — else a new identity made from the row's values and its display preferences. Core writes
    on the flush's connection, so it binds every session and runs before the row's INSERT."""
    from app.preferences import IDENTITY_PREFERENCE_KEYS

    if user.identity_id is not None or instance_dict(user).get("identity") is not None:
        return
    values = {field: user._pending_fields().get(field) for field in IDENTITY_FIELDS}
    if not values["email"]:
        raise ValueError("a membership is made with its person's email or names its identity")
    identities = UserIdentity.__table__
    existing = connection.execute(
        select(identities.c.id)
        # Both sides fold in Postgres, as revision 0360's trigger does: Python's `str.lower` differs on
        # some Unicode (final sigma), which would let a case variant of a live email make a second person.
        .where(func.lower(identities.c.email) == func.lower(values["email"]))
        .order_by(identities.c.created_at, identities.c.id)
        .limit(1)
        .with_for_update()
    ).scalar()
    if existing is None:
        held = user.preferences if isinstance(user.preferences, dict) else {}
        if any(key in held for key in IDENTITY_PREFERENCE_KEYS):
            # The display keys are the person's alone (`app/preferences.py`): they move, not copy.
            user.preferences = {key: value for key, value in held.items() if key not in IDENTITY_PREFERENCE_KEYS}
        actors = {
            column: getattr(user, column)
            for column in ("created_by_id", "updated_by_id")
            if getattr(user, column) is not None
        }
        user.identity_id = connection.execute(
            insert(identities)
            .values(
                **values, **actors,
                preferences={key: held[key] for key in IDENTITY_PREFERENCE_KEYS if key in held},
            )
            .returning(identities.c.id)
        ).scalar_one()
    elif connection.execute(
        select(AdminUser.__table__.c.id).where(AdminUser.__table__.c.identity_id == existing).limit(1)
    ).first():
        connection.execute(text(_EMAIL_HELD))
    else:
        connection.execute(update(identities).where(identities.c.id == existing).values(**values))
        user.identity_id = existing


def assert_member_row_not_superadmin(user: AdminUser) -> None:
    """The invariant a membership made by an invitation accept or a self-create holds: never
    `is_superadmin`, so operator power exists only on an operator-plane row (`authz.Principal.is_operator`).
    Raises before the row can be written."""
    if user.is_superadmin:
        raise ValueError("a membership made by an invitation or a self-create is never is_superadmin")


class UserMfaFactor(Base):
    """A PERSON's TOTP second factor (G-01, revision 0315); at most one per person (`identity_id`, revision
    0361), so it applies to every membership they hold. The flow is `app/mfa.py`.

    `confirmed_at` NULL is an enrolment in progress, which satisfies nothing. `secret_sealed` is the base32
    secret sealed under the tier-1 key (`app/credential_crypto.py::seal`). `last_used_step` is the newest
    accepted RFC 6238 time-step: a step at or below it is a replay. `pending_nonce` names the one live
    pending-second-factor cookie; a new sign-in replaces it. `pending_failures` counts the user's bad codes
    since their last good one and `last_failure_at` dates the newest (`app/mfa.py::FAILURE_WINDOW`);
    `step_up_failures`/`step_up_last_failure_at` are the same for a signed-in step-up (`app/mfa.py::verify_current_code`),
    counted apart so a step-up's bad codes never throttle sign-in."""

    __tablename__ = "user_mfa_factors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The membership that enrolled; NULL once it is deleted — the factor is the person's and outlives it.
    # Kept until revision 2 of the identity contraction.
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    # The PERSON whose factor this is: one per person, read by every membership, and its owner.
    identity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # The membership id the secret's AAD is keyed on (`app/mfa.py::_aad`): set once, never a reference.
    key_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    secret_sealed: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_used_step: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    pending_nonce: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pending_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_failure_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    step_up_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    step_up_last_failure_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class PersonRowMixin:
    """The two owners of a per-person security row (a recovery code, a trusted device, a password reset):
    `identity_id`, the PERSON the row belongs to and its owner, cascading with them; `user_id`, the
    membership that acted, NULL once it is deleted (the row is the person's). `user_id` is kept until revision
    2 of the identity contraction."""

    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    identity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False, index=True
    )


class UserRecoveryCode(PersonRowMixin, Base):
    """One single-use recovery code of a person (revision 0315; `identity_id`, revision 0361). Only
    `code_hash` is stored (`app/mfa.py::_code_hash`, keyed on the row's `key_user_id`); `used_at` set is spent. A
    regeneration or an MFA reset deletes the person's whole set."""

    __tablename__ = "user_recovery_codes"
    __table_args__ = (UniqueConstraint("user_id", "code_hash", name="uq_user_recovery_codes_user_hash"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The membership id the code's hash is keyed on (`app/mfa.py::_code_hash`): set once, never a reference.
    key_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class UserTrustedDevice(PersonRowMixin, Base):
    """A browser its person (`identity_id`, revision 0361) chose to trust after proving the second factor (revision 0340): signing in there
    skips the code until `expires_at`. Only `token_hash` is stored; the token lives in the browser's
    `app/mfa.py::DEVICE_COOKIE`. A password change, a disable (`app/mfa.py::revoke_trusted_devices`) or an MFA
    reset (`app/mfa.py::reset`) deletes every one of the person's devices; a sign-out does not. `label` names the browser for the list."""

    __tablename__ = "user_trusted_devices"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_user_trusted_devices_token_hash"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = timestamp_column(server_now=False)
    last_used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class TelegramLink(Base):
    """One Telegram chat a PERSON (`identity_id`) receives notifications in — a private chat or a group the bot was
    added to — or the one-time code that will prove one. `app/telegram_link.py` owns every rule: the console issues a
    code (`code_hash`, only the hash, until `code_expires_at`) on a row with no chat yet (one per person,
    `uq_telegram_links_identity_pending`),
    the person opens the bot's deep link (`start` for a private chat, `startgroup` for a group), and the webhook
    stores the chat Telegram names (`chat_id`, `chat_type`, `linked_at`). A person may link several chats, each once
    (`uq_telegram_links_identity_chat`). Per person, so one link serves every org they act in; which org sends it
    anything is each membership's own Telegram channel at it (`notification_channels`). `code_admin_user_id` is the
    membership that asked, which the redeem adds a channel for; NULL once that membership is deleted.
    Machine-written, so no entity shape."""

    __tablename__ = "telegram_links"
    __table_args__ = (
        Index(
            "uq_telegram_links_identity_chat", "identity_id", "chat_id", unique=True,
            postgresql_where=text("chat_id IS NOT NULL"),
        ),
        Index(
            "uq_telegram_links_identity_pending", "identity_id", unique=True,
            postgresql_where=text("chat_id IS NULL"),
        ),
        Index(
            "uq_telegram_links_code_hash", "code_hash", unique=True,
            postgresql_where=text("code_hash IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    identity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False
    )
    # NULL until the person opens the deep link; Telegram's chat id, which can exceed 32 bits (a group's is negative).
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Telegram's `chat.type`: `private`, `group` or `supergroup`; NULL while pending.
    chat_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    linked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The chat's name as Telegram gives it — a private chat's sender (`@username`, else their name), a group's
    # title — so the console can show WHICH chat is connected.
    chat_label: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    code_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    code_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    code_admin_user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class NotificationEmail(UuidPkMixin, Base):
    """An email address one PERSON (`identity_id`) typed to receive notifications at, and the proof it is theirs.

    Per person, as `TelegramLink`: one verified address serves every org they act in, and which org sends it
    anything is each membership's own Email channel (`notification_channels.email_id`). `address` is lower-cased
    through `mail_message.single_address` before it is stored. Only the HASHES of the code and the link token are
    kept, each until its expiry; `attempts`, `last_sent_at` and `sends_in_window` bound guessing and resending.
    Machine-written, so no entity shape."""

    __tablename__ = "notification_emails"
    __table_args__ = (UniqueConstraint("identity_id", "address", name="uq_notification_emails_identity_address"),)

    identity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False
    )
    address: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    code_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    code_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    link_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_sent_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    sends_in_window: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class PasswordReset(UuidPkMixin, PersonRowMixin, Base):
    """One password-reset token (revision 0351). Only `token_hash` (sha256) is stored; the token lives in the
    emailed link. `used_at` set is spent; a token is good until `expires_at`. The PERSON's (`identity_id`,
    revision 0361): the password it sets is theirs, and a reset spends every open link of theirs. `user_id`
    is the membership the forgot door picked, whose eligibility the link is checked against. `requested_ip`
    is who asked, for the audit."""

    __tablename__ = "password_resets"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_password_resets_token_hash"),)

    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = timestamp_column(server_now=False)
    used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    requested_ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class LegalAcceptance(UuidPkMixin, Base):
    """One user accepting the Terms of Service and the Privacy Policy (revision 0351): which version
    (`legal_version`, the operator setting of that name at the time), the two URLs it pointed at, when, and
    from where. Append-only: a later version is a new row, never an edit. The PERSON's (`identity_id`, revision
    0361); `user_id` is the membership they accepted as. No entity shape (`tests/test_entity_shape.py`)."""

    __tablename__ = "legal_acceptances"

    # The membership that acted; NULL once it is deleted (the row is the person's). Kept until revision 2 of
    # the identity contraction.
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The PERSON this row belongs to, and its owner.
    identity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    legal_version: Mapped[str] = mapped_column(Text, nullable=False)
    terms_url: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_url: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at: Mapped[datetime] = timestamp_column(server_now=False)
    ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LoginFailure(Base):
    """One counted refused sign-in, keyed by the hashed identifier and the source IP, shared across peers so
    the lockout (`app/login_lockout.py`) holds fleet-wide. Never a raw identifier: the activity row
    (`login_failed`) is the readable record. Hour-old rows are pruned by the peer heartbeat."""

    __tablename__ = "login_failures"
    __table_args__ = (
        Index("ix_login_failures_identifier_hash_failed_at", "identifier_hash", "failed_at"),
        Index("ix_login_failures_failed_at", "failed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    identifier_hash: Mapped[str] = mapped_column(Text, nullable=False)
    source_ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failed_at: Mapped[datetime] = timestamp_column(server_now=False)
