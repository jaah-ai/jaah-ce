"""The declarative base every mapped class registers on, its shared mixins and column helpers."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.entity_shape import SYSTEM_ACTOR_ID


class Base(DeclarativeBase):
    pass


def _actor_column() -> Mapped[UUID]:
    """One actor column (`created_by_id`/`updated_by_id`): `EntityMixin`'s, and the same spelling on a table that
    carries the actor columns without the whole entity shape (a binding, a child record, an opt-in — each exempt
    in `tests/test_entity_shape.py`)."""
    return mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )


def timestamp_column(
    *, nullable: bool = False, server_now: bool | None = None, onupdate: bool = False, index: bool = False
) -> Mapped[datetime]:
    """One timezone-aware timestamp column: the single spelling of every `*_at` in the tree (`app/models/`
    and the feature model modules that import it) but one — `AdminUser._legacy_email_verified_at` names its
    database column explicitly, which this helper does not take.

    `server_now` stamps the row at insert with the database's `now()`; it defaults to the column's
    nullability — a NOT NULL stamp is taken at insert, a nullable one is "not yet" until a writer sets it —
    and a NOT NULL column with `server_now=False` is one the writer must supply (an expiry, a window start).
    `onupdate` re-stamps on every ORM update: NON-entity tables only, since `EntityMixin.updated_at` is the
    `set_updated_at` trigger's (`app/entity_shape.py`) and `tests/test_entity_shape.py` holds every entity
    class to that."""
    if server_now is None:
        server_now = not nullable
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        **({"server_default": func.now()} if server_now else {}),
        **({"onupdate": func.now()} if onupdate else {}),
        **({"index": True} if index else {}),
    )


class EntityMixin:
    """The seven columns every ENTITY table carries. `tests/test_entity_shape.py` is the single home of the exempt set.

    Deliberately absent:
    * `name` uniqueness — per-entity (`groups` needs it, `worktrees` cannot have it).
    * a `server_default` on `name` — a raw-SQL writer that forgets to name its row fails at insert
      rather than shipping an unreadable label.
    * `onupdate=` on `updated_at` — the `set_updated_at` trigger owns it (`app/entity_shape.py`);
      `onupdate` skips every raw-SQL path.

    The two PERSON tables (`UserIdentity`, `AdminUser`) carry the shape MINUS `name`: a person's label is
    derived from the structured name pair (`app/identities.py::person_label`), so each unmaps
    the mixin's column (`name = None`) and `tests/test_entity_shape.py::_PERSON_TABLES` holds them to that.

    The actor columns DO carry a DB default (the seeded non-loginable `is_system` actor) so raw-SQL
    writers do not fail on `NOT NULL`; `app/actors.py::stamp_actor` attributes operator mutations.
    `created_by_id`/`updated_by_id` are never the OBJECT access decision (role bindings, `app/access.py`);
    `Project.created_by_id`'s comment is the single home for what a creator column DOES decide."""

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = _actor_column()
    updated_by_id: Mapped[UUID] = _actor_column()


class UuidPkMixin:
    """The DB-minted uuidv7 primary key of every table that is not an entity (`EntityMixin` carries its own
    copy so `tests/test_sessions_schema.py` can read the entity shape off that class's own annotations)."""

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))


class StampedMixin:
    """The two timestamps and two actor columns without the rest of the entity shape: a binding, a child record, an
    opt-in — each exempt in `tests/test_entity_shape.py`. No `set_updated_at` trigger comes with it; a table either
    has the trigger (an entity) or its writer stamps `updated_at`."""

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = _actor_column()
    updated_by_id: Mapped[UUID] = _actor_column()


class OrgScoped:
    """The tenancy column, on every class `app/tenancy/manifest.py` calls tenant-owned. NOT NULL.

    `Stored` and `Derived` classes both carry it: a derived class's org is held equal to its parent's by
    the composite key `(parent_id, org_id) REFERENCES parent(id, org_id)`. `test_tenancy_org_column.py`
    and `test_tenancy_org_schema.py` hold the census and the database to this.

    NOT NULL gives those keys meaning (MATCH SIMPLE passes on any NULL). A second tenant-owned reference
    (`tasks.project_id`) agrees only where `app/tenancy/manifest.py` declares a `same_org` key for it; the
    references left unkeyed are registered in the api's composite-keys tenancy test.

    No `ForeignKey` to `orgs` (deferred for locking budget), so a nonexistent org is storable;
    `app/tenancy/create_surface.py::named_org` checks on create. The annotation stays `Optional` so every
    comparison keeps its `is not None` guard (`app/placement_grants.py::placement_org`); the DDL holds
    the guarantee."""

    org_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=False)


def org_unique(table: str) -> UniqueConstraint:
    """`UNIQUE(id, org_id)` — what a composite foreign key `(parent_id, org_id)` must target.

    Required even though redundant with the PK. Declared on every `Stored` class;
    `app/tenancy/keys.py::same_org_keys` refuses a parent without it. Built CONCURRENTLY first against a live CP
    (`app/tenancy/contraction.py::apply_unique_constraint`)."""
    return UniqueConstraint("id", "org_id", name=f"uq_{table}_id_org_id")


def hnsw_cosine_index(name: str, column: str) -> Index:
    """The pgvector HNSW cosine index an embedding column is searched through (`m=16, ef_construction=64`)."""
    return Index(
        name, column, postgresql_using="hnsw", postgresql_ops={column: "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )


def _in_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")", name=name)
