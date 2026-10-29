"""Credentials: provider accounts, credential rows, NodeVault keys and grants, cloud and git connections."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
# Stdlib-only leaves: the ticket and email-template CHECKs render from their vocabularies.
from app import account_auth
from app.crons import in_domain_sql
from app.forges import FORGES
from app.models.base import (
    _actor_column,
    Base,
    EntityMixin,
    org_unique,
    OrgScoped,
    StampedMixin,
    timestamp_column,
    UuidPkMixin,
)


class ProjectCredential(OrgScoped, Base):
    """Join row: a credential attached to a project (0..N projects per credential). Both ends cascade."""

    __tablename__ = "project_credentials"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="CASCADE"), primary_key=True
    )
    created_by_id: Mapped[UUID] = _actor_column()
    created_at: Mapped[datetime] = timestamp_column()


class Account(EntityMixin, OrgScoped, Base):
    """Durable fleet credential-account roster — the DB SSOT for the accounts runners auth with.

    The console provisions rows; the jaahd dispatcher reads the enabled set and credential dir from the
    DB, never disk, and gets no quota. No `config_dir` column (0109): it is per node, derived by
    `app/node_clone.py::resolve_auth_dir(node.jaah_home, account.slug)`, which fails closed to `""`
    (`account_observations.config_dir` is what a node reported, a different fact). The console owns
    identity and `enabled`; quota is sensed. Provisioning creates the record, not the login.

    An entity (`EntityMixin`, 0109). `slug` materializes at `{jaah_home}/auth/{slug}`, so it is NOT NULL, unique
    and not renameable (a rename would move a live login's dir). `name` is the display label."""

    __tablename__ = "accounts"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("slug", name="uq_accounts_slug"),
        # An API-key account without a credential pointer is undispatchable, so the pairing is a CHECK.
        # Additive: existing rows are `oauth`.
        CheckConstraint(
            "auth_kind <> 'api_key' OR credential_id IS NOT NULL",
            name="ck_accounts_api_key_has_credential",
        ),
        # A cap of zero or less would be an account that can never run, spelled as a number (0277).
        CheckConstraint(
            "max_runners IS NULL OR max_runners > 0",
            name="ck_accounts_max_runners_positive",
        ),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    # Personal ⇔ NOT NULL: the user this row belongs to. SET NULL on their delete hands it to the org,
    # where only an Org Owner/Admin reaches it — never wider than before.
    owner_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    # How this account authenticates (`app/account_auth.py` owns the vocabulary). Text with a server
    # default, not an enum, so a new kind costs a constant; the default keeps old rows meaning oauth.
    auth_kind: Mapped[str] = mapped_column(
        Text, nullable=False, default=account_auth.AUTH_KIND_OAUTH,
        server_default=account_auth.AUTH_KIND_OAUTH,
    )
    # The pointer, never the secret: the key is a `credentials` row (the provider's
    # `api_key_credential_type`) read over the node's credential socket at dispatch. NULL for OAuth; required for API-key by the CHECK. RESTRICT,
    # not SET NULL (which would write a row the CHECK forbids): disable the account before deleting.
    credential_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT"), nullable=True
    )
    # Which vendor's CLI this credential logs into; names live in `apps/jaahd/agent_providers.py` (R2.1). NOT
    # NULL, default `'claude'` (additive). Open vocabulary, no CHECK. Immutable after creation, like
    # `slug`: the login on disk is vendor-specific; `AccountUpdateIn` omits it and
    # `app/routes/accounts.py::update_account` refuses a change.
    provider: Mapped[str] = mapped_column(
        Text, nullable=False, default="claude", server_default="claude"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Whether the scheduler may pick this account for a task that did not ask for it (0277). The server
    # default keeps every pre-existing row picked as before; a new row's value comes from its provider's
    # `auto_assign_default` when the create request names none (`routes/accounts.py::create_account`).
    auto_assign: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # This account's own concurrent-runner cap (0277); NULL = none. Accepted only for a provider declaring
    # `per_account_runner_cap` (DeepSeek: no quota to gate on, so this is what bounds its spend).
    max_runners: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Unread by current nodes (they find the token themselves on the NodeLink).
    # Kept for older jaahd packages during rollout: the roster still serves it as `shared`, written from what a
    # node reports after a login (`routes/commands.py::_ensure_account_row`). Drop once every node is current.
    shared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Which node the credential dir lives on; NULL = unscoped/fleet-wide. SET NULL: a deleted node
    # leaves the account fleet-wide.
    node_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    # Live: the runner-break verdict store, written only by `account_fold.record_account_verdict` (always
    # with a deadline), read via `account_fold.resolved_quota_status`.
    # `ck_accounts_quota_status_has_a_deadline` forbids a NULL deadline, so manual UPDATEs set both.
    quota_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When `quota_status` stops being believed: the verdict only bridges the ~60s quota-mirror lag. NULL
    # is a fossil `standing_verdict` ignores; both readers apply it through `standing_verdict`.
    quota_status_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The break's sentence behind `quota_status` (redacted, capped), written with it; served only while it stands.
    quota_status_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Retired with the three percentages above; same drop conditions.
    quota_updated_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When the account first held a working credential. NULL only on an Add-Account sign-in's phase-1 row
    # (`routes/commands.py::_ensure_account_row`), stamped by its phase 2; every other path takes the default.
    # A login ending unfinished drops a row still NULL (`durable/accounts.py::drop_abandoned_row`).
    signed_in_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True, server_now=True)
    # When a node holding this account's session was deleted, taking the session along
    # (`node_account_sessions.mark_sessions_lost`). Never cleared: the Accounts page reads it only while no node
    # holds a session, so a later sign-in hides it.
    node_removed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Entity columns come from `EntityMixin`; no `onupdate`: `trg_accounts_set_updated_at` (0109) owns
    # `updated_at`, since raw SQL writes this table too (`account_fold._write_account_verdict`).


# ── The state plane ───────────────────────────────────────────────────────────────────────────────────
# One typed table per retired KV family, never a generic `kv` table; a genuine bag is a `jsonb` column.


class AccountObservation(OrgScoped, Base):
    """One node's observation of one credential account. The cross-node fold is the `account_fold` VIEW,
    read through `app/account_fold.py`.

    `quota` is jsonb because the fold reads it as a block (maxed across nodes, `resetsAt` following
    each argmax, collapsing on `auth_rejected`); `extra` is a pass-through so new identity fields reach
    the page. Only fields the fold decides on are typed columns.
    """

    __tablename__ = "account_observations"
    __table_args__ = (
        UniqueConstraint("node_id", "slug", name="uq_account_observations_node_slug"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # CASCADE (0106): re-derived every 15s pass.
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    # The sensed key, what the node reported. `fleet_observe.py`'s `ON CONFLICT` targets
    # `(node_id, slug)`; keying on the nullable `account_id` would never conflict and insert a row per
    # pass.
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    # The resolved roster pointer beside `slug`. Nullable and not in the key because ingest fails open
    # (else `attested_at` would stall); re-resolved every pass. SET NULL.
    account_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    config_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # NULL says nothing about enabled (the fold defaults True); an observed False always wins.
    enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # `none_as_null=True` is required: otherwise Python `None` becomes jsonb `null`, the upsert's
    # `excluded.quota IS NULL` carry-forward never fires, and quotas (and a live `auth_rejected`) are
    # blanked on probe-less passes.
    quota: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    # The node's pass clock, clamped only against forward skew: when the numbers were true
    # (`account_fold.quota_is_stale`), not whether the node still attests (see `attested_at`).
    observed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # First seen, not last heard (the upsert never updates it); not a liveness stamp.
    ingested_at: Mapped[datetime] = timestamp_column()
    # Last heard, CP-stamped `now()` on insert and update (0099): the only stamp the dispatch freshness
    # window may read, so a slow node clock cannot drop its own accounts. DB-stamped, DB-compared.
    attested_at: Mapped[datetime] = timestamp_column()


class CredentialOAuthFlow(OrgScoped, Base):
    """One in-flight credential OAuth wizard: the CP's record of a login a node is running for a user.

    RETIRED, NOT YET DROPPED: the node-CLI credential wizard retired with its one driver, so no
    route reads or writes this table. It stays mapped until a later release drops it,
    since a migration must stay additive while the outgoing release still maps it.

    Holds no tokens and no authorization code, by design: `credentials` is the hardened secret store
    (see `Credential`), and a second home would inherit none of its protections. Secrets live only in
    the node process and, after one in-memory hop, the `credentials` row; the pasted code is dispatched
    to the node and redacted by `routes/commands.py::_credential_safe_args`. Columns are only what the
    completing request reads.

    `user_id` is the scope, not provenance: every read filters on it. Not an entity (exempt in
    `tests/test_entity_shape.py`). One live row per user is a DB constraint
    (`uq_credential_oauth_flows_live_per_user`, partial on `error IS NULL`, migration 0175): the start
    route awaits between reading and replacing flows, so only the index stops two concurrent starts from
    spawning two vendor CLIs (the route retries on its failure). It is also why start has no node-grant
    check.
    """

    __tablename__ = "credential_oauth_flows"

    # Declared here too so autogenerate does not propose dropping the partial unique index (see the
    # class docstring); spelled as migration 0175.
    __table_args__ = (
        Index("ix_credential_oauth_flows_user_id", "user_id"),
        Index(
            "uq_credential_oauth_flows_live_per_user", "user_id",
            unique=True, postgresql_where=text("error IS NULL"),
        ),
    )

    # Minted by the start route with `uuid4`, not `uuidv7()`: the handle goes to a node and a browser,
    # and v7 leaks its creation time; server-side so a caller cannot address another session's login.
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    # CASCADE: a flow whose node is gone can never complete.
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    # CASCADE, not SET NULL: this is the scope, and a NULL would match any user's completing request.
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    # A `CREDENTIAL_TYPES` slug; text, like `Credential.type` (`app/credentials/registry.py`).
    type_slug: Mapped[str] = mapped_column(Text, nullable=False)
    # The `OAUTH_WIZARDS` driver, recorded so the flow finishes on the driver it started on.
    driver: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL = live, non-NULL = spent: the whole state machine. A spent row rests because the node's login
    # subprocess may live on and `credential_oauth_cancel` needs the pointer; a success is deleted in the
    # credential-writing transaction. Value: `"<code>"` or `"<code>: <detail>"`, which the completing
    # route's 502 and a repeat `complete` both carry.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # No clock: nothing reads one (the per-user replacement reaps regardless of age). The partial index
    # and the caller's next start bound this table.


class ConsoleApiKeyLease(UuidPkMixin, OrgScoped, Base):
    """One console create's authorization to fetch an API-key account's key on one node (§3.5a, 0281).

    Written by `routes/console.py` only when a console create picks an API-key account, before the node
    is asked to open anything; read by `routes/agent/task_secrets.py::get_account_api_key` as its lease arm, which
    admits the account only while `expires_at` is in the future. Node-granular and not revoked on tab
    close — an accepted residual. `app/console_key_leases.py` owns the TTL and the sweep.
    Not an entity (exempt in `tests/test_entity_shape.py`): machine-written, never edited."""

    __tablename__ = "console_api_key_leases"
    __table_args__ = (
        # The arm's read: this node's unexpired leases.
        Index("ix_console_api_key_leases_node_expires", "node_id", "expires_at"),
        # The CASCADE from `accounts` and the sweep's `expires_at` cut.
        Index("ix_console_api_key_leases_account_id", "account_id"),
        Index("ix_console_api_key_leases_expires_at", "expires_at"),
    )

    # CASCADE: a lease names a node's right to fetch; with the node gone there is nothing to authorize.
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    # CASCADE: deleting the account ends access at once (disabling does too, via the route's own conjunct).
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    # The user whose console create wrote the lease — the audit stops at user + node. CASCADE with them.
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = timestamp_column()
    expires_at: Mapped[datetime] = timestamp_column(server_now=False)


class Credential(EntityMixin, OrgScoped, Base):
    """One configured connection to a third-party system.

    `config` holds the type-specific fields, keyed by `app/credentials/registry.py`'s `FieldSpec.key`s
    (which also says which are secret). It is a PROPERTY over the `config` JSONB column
    (`config_sealed`), encrypted at rest in two tiers (`app/credential_crypto.py`): reading it
    opens the non-secret tier under the env key and presents each secret as a `SealedSecret` marker (no
    KMS call); writing it seals — a `str` under a secret key is wrapped under the deployment's RSA public
    key, a `SealedSecret` is stored as-is. A secret's plaintext is reached only through
    `credential_crypto.open_secret(row, field)`, where it is consumed. Anything reaching the column
    without the ORM (raw `text()`, core `insert()`, a `->>` path) sees the sealed value;
    `tests/test_credential_crypto.py` keeps app code free of that.

    The row's id is part of every envelope's AAD, so it is minted BEFORE the first seal: a browser
    create carries its own UUID and a server-side create takes a `uuid4` here, never the DB default.

    Encryption does not relax the rules on the opened value: never in a log, dump or event (the CRUD
    router emits no console event), and `_credential_out` is the one serializer, omitting (not masking)
    secret keys.

    NAMING — the single home of the scheme. A credential has exactly ONE home, decided by its
    columns: system (`system`; on the operator plane that org reads Operator), org shared (no owner, not
    system, no home project), project (`home_project_id`) or personal (`owner_id`). The home is shown as a
    locked prefix (`credential_wire.py::_home_path`), never typed into the name. `name` is the leaf inside
    the home: stored lowercase and `/`-segmented (`credential_wire.py::_validated_name` normalizes; any
    typed text is accepted), unique case-insensitively per home on every write
    (`credential_wire.py::_refuse_duplicates_in_home`; no DB unique, so a pre-existing duplicate stays
    readable). Runners address a row by its scope-relative path — `org/<name>`,
    `projects/<lowercased project name>/<name>` or `me/<name>` (`routes/agent/task_secrets.py::runner_path`;
    a `/` in the project name is escaped as `%2f`, a `%` as `%25`), and nothing else in the system looks a
    credential up by name. The project segment is the project's NAME, so renaming a project changes the path
    of every row homed in it. No unique on `type` (multiple instances per type) and no `slug`: a credential
    materializes nowhere.
    An entity (`EntityMixin`) since 0132; actor columns are provenance only (access is `owner_id` plus role bindings,
    `app/access.py`).
    """

    __tablename__ = "credentials"
    # The list route's total order: `id DESC` breaks `created_at` ties (same-transaction rows) so OFFSET
    # pages neither repeat nor skip. A tiebreak only: ids converted in 0132 are not time-ordered.
    __table_args__ = (
        org_unique(__tablename__),
        Index("ix_credentials_created_at_id", text("created_at DESC"), text("id DESC")),
        CheckConstraint("NOT system OR owner_id IS NULL", name="ck_credentials_system_unowned"),
        # A project home is an ORG row's alone: never a personal or a system one (see NAMING above).
        CheckConstraint(
            "home_project_id IS NULL OR (owner_id IS NULL AND NOT system)",
            name="ck_credentials_home_project_org_row",
        ),
        Index(
            "ix_credentials_home_project_id", "home_project_id",
            postgresql_where=text("home_project_id IS NOT NULL"),
        ),
        # The roster at scale (revision 202610232130 spells each in SQL). Every sort order with
        # its `id` tiebreak, alone and under one org.
        Index("ix_credentials_lname_id", text("lower(name)"), "id"),
        Index("ix_credentials_type_lname_id", "type", text("lower(name)"), "id"),
        Index("ix_credentials_org_lname_id", "org_id", text("lower(name)"), "id"),
        # A tenant's roster by Type (revision 202610240200).
        Index("ix_credentials_org_type_lname_id", "org_id", "type", text("lower(name)"), "id"),
        Index("ix_credentials_org_created_id", "org_id", text("created_at DESC"), text("id DESC")),
        # The folder tree: one home's folders by prefix, a folder's rows in name order, a home's count.
        Index(
            "ix_credentials_home_parent_pat", "home_key", "parent_path",
            postgresql_ops={"parent_path": "text_pattern_ops"},
        ),
        Index("ix_credentials_home_parent_lname_id", "home_key", "parent_path", text("lower(name)"), "id"),
        Index("ix_credentials_home_key", "home_key"),
        # The tree's walks (revision 202610240600): one org's homes, then a home's folders.
        Index("ix_credentials_org_home_key", "org_id", "home_key"),
        # The folder walk's key (`routes/credentials/tree.py::FOLDER_KEY`): one range per folder.
        Index("ix_credentials_home_folder_key", "home_key", text("(parent_path || '/') text_pattern_ops")),
        # The Owner filter's expression (`routes/credentials/roster.py::ROSTER_OWNER`), newest first.
        Index(
            "ix_credentials_owner_expr", text("coalesce(owner_id, created_by_id)"),
            text("created_at DESC"), text("id DESC"),
        ),
        Index(
            "ix_credentials_name_trgm", text("lower(name)"),
            postgresql_using="gin", postgresql_ops={"lower(name)": "gin_trgm_ops"},
        ),
        Index(
            "ix_credentials_description_trgm", text("lower(description)"),
            postgresql_using="gin", postgresql_ops={"lower(description)": "gin_trgm_ops"},
        ),
        # The health filters: each a partial index walked newest first (expiry by its instant).
        Index(
            "ix_credentials_no_secret", text("created_at DESC"), text("id DESC"),
            postgresql_where=text("NOT has_stored_secret"),
        ),
        Index("ix_credentials_expires_at", "expires_at", postgresql_where=text("expires_at IS NOT NULL")),
        Index(
            "ix_credentials_never_used", text("created_at DESC"), text("id DESC"),
            postgresql_where=text("last_used_at IS NULL"),
        ),
        Index(
            "ix_credentials_test_failed", text("created_at DESC"), text("id DESC"),
            postgresql_where=text("last_test_ok = false"),
        ),
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)
    # Personal ⇔ NOT NULL: the user this row belongs to. SET NULL on their delete hands it to the org,
    # where only an Org Owner/Admin reaches it — never wider than before.
    owner_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    # A SYSTEM row (0325): an org row the control plane itself acts with. Only an Org Owner/Admin or an
    # operator reaches it, and it never reaches a runner (`app/authz/visibility.py::sees_system_credentials`).
    system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The PROJECT home: an org row homed in one project, counted as attached to it
    # (`authz/visibility.py::_pool_of`). SET NULL on the project's delete leaves it org shared.
    home_project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    # The sealed value (`credential_crypto.seal_config`'s shape) in the `config` column. The plaintext
    # `{}` server default only fires for a raw-SQL insert, and such a row is refused on read
    # (`open_config` has no pass-through), see
    # `tests/test_credential_crypto.py::test_no_reader_or_writer_in_app_code_goes_round_the_column_type`.
    config_sealed: Mapped[dict[str, Any]] = mapped_column(
        "config", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    # The user's NodeVault intent (0377, the second key provider beside KMS): every tier-2 envelope is sealed
    # under the org's NodeVault key, which only its nodes open. Invariant, held at every write by
    # `credential_crypto.check_nodevault_invariant` and refused at flush by its listener below:
    # `nodevault ⇔ every tier-2 envelope's k starts with "nv:"`. The key hash is the envelopes' `k`
    # (`credential_crypto.nodevault_key_hash`), never stored a second time.
    nodevault: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Health facts, stored so the roster filters on an index instead of opening every row. The
    # `config` setter writes the first two from the value it seals (`app/credentials/health.py` owns both
    # rules) and nothing else does. `true` by default: a row the outgoing release inserts reads as holding a
    # secret, as an unopenable one always has.
    has_stored_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # When the credential stops working (`health.expires_at`); NULL when it does not or cannot be told.
    expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The last node or runner fetch (`app/credentials/health.py::record_use`), and its last connection test's time and verdict.
    last_used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_tested_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # The row's home as one key (NAMING above: system, project, personal, else org shared) and its folder: the
    # lowercased name up to its last `/`, `''` at the home's root. Generated, so no writer can let them drift;
    # the `::text` casts keep the expressions IMMUTABLE, which a generated column requires.
    home_key: Mapped[str] = mapped_column(
        Text,
        Computed(
            "CASE WHEN system THEN 's:' || org_id::text"
            " WHEN home_project_id IS NOT NULL THEN 'p:' || home_project_id::text"
            " WHEN owner_id IS NOT NULL THEN 'u:' || owner_id::text"
            " ELSE 'o:' || org_id::text END",
            persisted=True,
        ),
    )
    parent_path: Mapped[str] = mapped_column(
        Text,
        Computed(
            "CASE WHEN strpos(name, '/') = 0 THEN ''"
            " ELSE substring(lower(name) from '^(.*)/[^/]*$') END",
            persisted=True,
        ),
    )

    def __init__(self, **kwargs: Any) -> None:
        # `config` is sealed under the row's id and type, so both are set before it whatever the kwargs
        # order; a server-side create gets its id here (see the docstring).
        config = kwargs.pop("config", None)
        super().__init__(**kwargs)
        # An omitted config is an EMPTY sealed config, never the raw `{}` the DB default would leave.
        self.config = {} if config is None else config

    @property
    def config(self) -> Optional[dict[str, Any]]:
        from app.credential_crypto import open_config

        if self.config_sealed is None:
            return None
        return open_config(self.config_sealed, credential_id=self.id)

    @config.setter
    def config(self, value: Optional[dict[str, Any]]) -> None:
        from app.credential_crypto import seal_config
        from app.credentials import health

        if value is None:
            self.config_sealed = None  # type: ignore[assignment]
            self.has_stored_secret, self.expires_at = False, None
            return
        if self.id is None:
            self.id = uuid4()
        if self.type is None:
            raise ValueError("a credential's type must be set before its config is sealed")
        self.config_sealed = seal_config(value, credential_id=self.id, type_slug=self.type)
        # Derived from the value just sealed, so every writer keeps them true (see the columns' note).
        self.has_stored_secret = health.has_stored_secret(self.type, value)
        self.expires_at = health.expires_at(self.type, value)


# ── NodeVault (0377; the second key provider beside KMS) ─────────────────────────────────────────────
#: `nodevault_keys.state`. LIVE (a node may be granted it, a seal may name it): pending, current, previous.
NV_KEY_PENDING, NV_KEY_CURRENT, NV_KEY_PREVIOUS = "pending", "current", "previous"


NV_KEY_RETIRING, NV_KEY_RETIRED, NV_KEY_ABANDONED = "retiring", "retired", "abandoned"


NODEVAULT_KEY_STATES = (
    NV_KEY_PENDING, NV_KEY_CURRENT, NV_KEY_PREVIOUS, NV_KEY_RETIRING, NV_KEY_RETIRED, NV_KEY_ABANDONED,
)


NODEVAULT_LIVE_KEY_STATES = (NV_KEY_PENDING, NV_KEY_CURRENT, NV_KEY_PREVIOUS)


#: The states a browser-sealed envelope may name at a credential write (`sealed_secret_from_wire`).
NODEVAULT_SEALABLE_KEY_STATES = (NV_KEY_CURRENT, NV_KEY_PREVIOUS)


NODEVAULT_KEY_SOURCES = ("generate", "upload")


#: `nodevault_grants.state` / `.blob_kind` / `.last_error` (a closed vocabulary: never key material).
NV_GRANT_WAITING, NV_GRANT_DELIVERED, NV_GRANT_FAILED = "waiting", "delivered", "failed"


NV_GRANT_RETIRE_PENDING, NV_GRANT_RETIRED = "retire_pending", "retired"


NODEVAULT_GRANT_STATES = (
    NV_GRANT_WAITING, NV_GRANT_DELIVERED, NV_GRANT_FAILED, NV_GRANT_RETIRE_PENDING, NV_GRANT_RETIRED,
)


NODEVAULT_BLOB_KINDS = ("node", "chain", "peer")


#: Transient (`hello` resets them) then permanent (an admin's Retry or a reset-identity does). The node's own
#: refusal words (`apps/jaahd/nodevault_commands.py::ERRORS`) plus the bus's `offline`/`timeout`/`rejected` (0386).
NODEVAULT_TRANSIENT_GRANT_ERRORS = ("offline", "timeout")


NODEVAULT_GRANT_ERRORS = NODEVAULT_TRANSIENT_GRANT_ERRORS + (
    "crypto_unavailable", "rejected",
    "hash_mismatch", "wrong_recipient", "missing_wrapping_key", "open_failed", "malformed",
)


#: `nodevault_operations.kind` / `.state` / `.step` (the union of every kind's steps, arch §3.4).
NV_OP_RUNNING = "running"


NODEVAULT_OPERATION_KINDS = ("setup", "rotation", "migration", "retire")


NODEVAULT_OPERATION_STATES = (NV_OP_RUNNING, "done", "failed")


NODEVAULT_OPERATION_STEPS = (
    "distribute", "promote", "migrate_choice", "migrating", "rekey", "retiring", "result",
)


#: `nodevault_operation_items.phase` / `.state`.
NODEVAULT_ITEM_PHASES = ("migrate", "rekey")


NODEVAULT_ITEM_STATES = ("pending", "sealed", "done", "skipped", "failed")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class NodeVaultKey(EntityMixin, OrgScoped, Base):
    """One NodeVault key of an org: its PUBLIC half only. The private half exists on the org's nodes and in
    the admin's passphrase backup, never in this database (AC-8.1).

    `key_hash` is `hex(sha256(SPKI DER))`, always recomputed from `public_key` by the CP
    (`credential_keys.nodevault_key_hash`), never taken from a caller. At most one `current` per org
    (`uq_nodevault_keys_one_current`). `chain_blob` is the rotation's new private key wrapped under the
    CURRENT key (ciphertext, E2). `name` is the shortened hash; no `slug` (reached by its hash)."""

    __tablename__ = "nodevault_keys"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "key_hash", name="uq_nodevault_keys_org_key_hash"),
        Index(
            "uq_nodevault_keys_one_current", "org_id", unique=True,
            postgresql_where=text(f"state = '{NV_KEY_CURRENT}'"),
        ),
        CheckConstraint(_sql_in("state", NODEVAULT_KEY_STATES), name="ck_nodevault_keys_state"),
        CheckConstraint(_sql_in("source", NODEVAULT_KEY_SOURCES), name="ck_nodevault_keys_source"),
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="ck_nodevault_keys_key_hash"),
    )

    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Base64 SPKI DER, as WebCrypto exports it.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default=NV_KEY_PENDING, server_default=text(f"'{NV_KEY_PENDING}'")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    chain_blob: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    backup_confirmed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    retired_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class NodeVaultGrant(UuidPkMixin, OrgScoped, Base):
    """Where one NodeVault key should be and is: one row per (node, key), the table the delivery and
    retirement workflows and the UI read (`app/nodevault_grants.py` is its one enrolment writer).

    `blob` is ciphertext only (a key blob wrapped to the node, the chain, or by a peer), NULL until
    produced. `generation` numbers the workflow id `nodevault-<deliver|retire-node>:{node}:{key}:{gen}`, so a
    finished-but-unsettled workflow is superseded by a fresh id rather than re-run. Not an entity: machine
    state the workflows advance, never edited by name."""

    __tablename__ = "nodevault_grants"
    __table_args__ = (
        UniqueConstraint("node_id", "key_hash", name="uq_nodevault_grants_node_key"),
        CheckConstraint(_sql_in("state", NODEVAULT_GRANT_STATES), name="ck_nodevault_grants_state"),
        CheckConstraint(
            "blob_kind IS NULL OR " + _sql_in("blob_kind", NODEVAULT_BLOB_KINDS),
            name="ck_nodevault_grants_blob_kind",
        ),
        CheckConstraint(
            "last_error IS NULL OR " + _sql_in("last_error", NODEVAULT_GRANT_ERRORS),
            name="ck_nodevault_grants_last_error",
        ),
        CheckConstraint("generation >= 0", name="ck_nodevault_grants_generation"),
        Index("ix_nodevault_grants_org_key_hash", "org_id", "key_hash"),
    )

    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    blob: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    blob_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default=NV_GRANT_WAITING, server_default=text(f"'{NV_GRANT_WAITING}'")
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    delivered_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class NodeVaultOperation(UuidPkMixin, OrgScoped, Base):
    """One NodeVault operation (setup, rotation, migration, retire), driving the console's stepper: the
    step is server-owned so a reload or a CP restart lands on the same one. ONE running operation of any
    kind per org (`uq_nodevault_operations_one_running`). `counts` holds numbers only. Not an entity: a
    workflow's record, never edited by name."""

    __tablename__ = "nodevault_operations"
    __table_args__ = (
        org_unique(__tablename__),
        Index(
            "uq_nodevault_operations_one_running", "org_id", unique=True,
            postgresql_where=text(f"state = '{NV_OP_RUNNING}'"),
        ),
        CheckConstraint(_sql_in("kind", NODEVAULT_OPERATION_KINDS), name="ck_nodevault_operations_kind"),
        CheckConstraint(_sql_in("state", NODEVAULT_OPERATION_STATES), name="ck_nodevault_operations_state"),
        CheckConstraint(_sql_in("step", NODEVAULT_OPERATION_STEPS), name="ck_nodevault_operations_step"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default=NV_OP_RUNNING, server_default=text(f"'{NV_OP_RUNNING}'")
    )
    step: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prev_key_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    workflow_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class NodeVaultOperationItem(UuidPkMixin, OrgScoped, Base):
    """One credential's result inside an operation (the Result step and its Retry). `staged` holds the
    migrate step's NodeVault envelopes between `seal` and `swap` — CIPHERTEXT ONLY, nulled on swap; never a
    value. `reason` is the workflow's closed vocabulary (`app/durable/nodevault.py`)."""

    __tablename__ = "nodevault_operation_items"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "credential_id", "phase", name="uq_nodevault_operation_items_op_credential_phase"
        ),
        CheckConstraint(_sql_in("phase", NODEVAULT_ITEM_PHASES), name="ck_nodevault_operation_items_phase"),
        CheckConstraint(_sql_in("state", NODEVAULT_ITEM_STATES), name="ck_nodevault_operation_items_state"),
        Index("ix_nodevault_operation_items_credential_id", "credential_id"),
    )

    operation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodevault_operations.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="CASCADE"), nullable=False
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    staged: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class CredentialIdTombstone(Base):
    """A hard-deleted credential's id, kept so the id is never minted for a row again (revision 0284).

    The row id is part of every sealed secret's AAD (`Credential.config`) and a create takes
    its id from the client. Without the tombstone, a holder of a deleted row's envelope (a backup, a
    dump) and `credentials:create` re-creates a row under the same id, type and destination URL, pastes
    the envelope, and its AAD matches again: their own task then opens the deleted row's secret.
    `routes/credentials/writes.py::delete_credential` writes the tombstone in the delete's own transaction;
    `create_credential` refuses a tombstoned id with the same 409 as a live one.

    Never read for anything else and never pruned: an id is 16 bytes and deletes are rare. Not an entity
    (`tests/test_entity_shape.py::_EXEMPT_TABLES`) and `Shared` (`app/tenancy/manifest.py`): the id
    space is deployment-wide, exactly like the primary key it guards, so an org-scoped tombstone would
    let another org re-mint the id.
    """

    __tablename__ = "credential_id_tombstones"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    deleted_at: Mapped[datetime] = timestamp_column()


class CloudConnection(EntityMixin, OrgScoped, Base):
    """A cloud connection: everything a control-plane launch of a cloud node needs, in one row — the
    credential that signs, the region, and the network and image choices inside that account.

    The credential is one of the connection's parameters, not its owner: one account may hold several
    connections (a region each, a VPC each), so `credential_id` is a plain index. CASCADE: without its
    account the row is useless. `provider` names the cloud; only `aws` launches today (the route refuses
    the rest), the column exists so GCP/Azure rows need no migration.

    `ami_id` `''` means "resolve at launch": every launch takes Canonical's newest `ec2.UBUNTU_RELEASE` image
    for the instance type's architecture (`app/cloud_node_launch.py::resolve_image`), and a non-empty
    value is an operator's pinned override; `ami_pinned_at` is when that pin was set (NULL when unpinned). `subnet_id`/`security_group_id` are resolved when the row is
    SAVED (`app/cloud_connection_network.py`) so the row shows exactly what a launch uses; `''` never
    persists past a save. `extra_subnet_ids` are more subnets of `subnet_id`'s VPC (checked at save), so
    a launch can spread across zones and fall back when one zone has no capacity
    (`app/cloud_node_launch.py::launch_cloud_node`); `subnet_ids` is the whole ordered list.

    `__tablename__` is still `cloud_node_defaults`: the rename to `cloud_connections` waits a release,
    since migrations stay additive while two releases overlap and the previous one reads this name.

    An entity (0165); `name` is the operator's label. No `slug`: reached by `id`.
    """

    __tablename__ = "cloud_node_defaults"

    # The cloud this connection launches on. Text, not an enum: a third value is a row, not a migration.
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="aws")
    # The account, named indirectly (see the class docstring). Indexed, not unique: many connections per
    # credential (revision 0404 dropped `uq_cloud_node_defaults_credential`).
    credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credentials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NOT NULL, no default: no implicit region.
    region: Mapped[str] = mapped_column(Text, nullable=False)
    # `''` = newest LTS image at launch; a pinned `ami-…` id otherwise (see the class docstring).
    ami_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # When `ami_id` last changed to a pin (`routes/cloud_nodes.py::_ami_pin_fields`); NULL while unpinned.
    ami_pinned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Resolved ids after a save; `''` only on the wire ("auto"), never in a saved row.
    subnet_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    security_group_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # More subnets of `subnet_id`'s VPC, in order, never holding `subnet_id` itself; `{}` = one subnet.
    extra_subnet_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    # Unread: a node's share is its org's NodeLink store (`org_shared_stores`). Dropped in a later release
    # (additive while two releases overlap).
    shared_efs_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    shared_efs_access_point_id: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    shared_efs_mount_ip: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # `''` means no IAM role: normal for a node outside our account, which reaches the CP with its own
    # `NODE_TOKEN`. Kept on the API; the console no longer offers it.
    instance_profile: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    root_volume_gb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="50")
    # The provider's own network choices, resolved at save (`app/clouds/base.py::CloudProvider.resolve_network`;
    # revision 202610211437). AWS keeps `subnet_id`/`security_group_id`/`extra_subnet_ids` above; `{}` on its rows.
    network: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    @property
    def subnet_ids(self) -> list[str]:
        """Every subnet a launch may use, primary first. `None` extras read as none: an unflushed row has
        no column default yet."""
        return [self.subnet_id, *(self.extra_subnet_ids or [])]


class UserGitConnection(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """Which git credential one person's work uses on one forge root.

    One row per `(user, root)`: `root` is `forges.RepoRef.root` (`github.com`, `gitlab.com`,
    `bitbucket.org`, `dev.azure.com`, or a self-managed GitLab's `host[/sub-path]`), and `forge` the
    forge that root belongs to (`forges.forge_of_root`). `app/git_access.py` reads it, re-authorizing the
    credential on every read, so a row is a preference, never an authorization.

    `admin_users.git_credential_id` is the pre-0291 single pointer, KEPT while two releases overlap: read
    falls back to it only when its credential's root is the repo's root; writes and deletes here keep it
    in step (`app/git_connections.py` is the single writer of both). Both FKs CASCADE: a deleted person
    or credential takes the connection with it. Not an entity (exempt in `tests/test_entity_shape.py`):
    the (user, root) pair is its identity, and a `name` would invent a second label."""

    __tablename__ = "user_git_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "root", name="uq_user_git_connections_user_root"),
        CheckConstraint(in_domain_sql("forge", FORGES), name="ck_user_git_connections_forge"),
        # The reverse lookup for a credential delete (Postgres does not index the referencing side).
        Index("ix_user_git_connections_credential_id", "credential_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    root: Mapped[str] = mapped_column(Text, nullable=False)
    forge: Mapped[str] = mapped_column(Text, nullable=False)
    credential_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="CASCADE"), nullable=False
    )
