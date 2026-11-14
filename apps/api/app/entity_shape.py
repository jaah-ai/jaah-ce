"""The standard entity shape's shared parts, including the ones a migration needs, not just the ORM.

`EntityMixin` (app/models/) gives the ORM the columns; this module single-homes what a revision
cannot get from the ORM: `SYSTEM_ACTOR_ID`, `new_uuid7()`, the `set_updated_at` trigger DDL.

A trigger, not SQLAlchemy's `onupdate=`, because `onupdate` skips every raw-SQL path. One shared
`public.set_updated_at()` plus one `trg_<table>_set_updated_at` per table; downgrade order: see
`set_updated_at_drop_function_sql`.

The wire side, shared by every entity route: `StrictModel`, `iso_or_none`, `EntityOut`/`entity_fields`,
`RefOut`/`UuidRefOut` (a row as a filter option or a link names it), `partial_of` (a Patch body derived from its
Create twin), `from_row` (an Out model that is
mostly a straight copy of its row), `pick` (the same for a dict literal), `validated_name`/`validated_description`
(the one 422 grammar), and
`optional_uuid`/`get_or_404`/`count_by`/`names_of`/`all_names_of`/`row_by_uuid_or_404`/`flush_or_409`/`refused_as`/`constraint_name`,
the write verbs' tail
`commit_row` and null-field guard `reject_cleared`, `unique_org_slug` (raced safely by `add_with_org_slug`), and the
Postgres integer bounds `PG_INT_MIN`/`PG_INT_MAX`/`PG_BIGINT_MAX` an id or count is checked against before it is bound.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Optional, TypeVar, Union, get_args, get_origin
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.tenancy.ids import is_tenant_org

if TYPE_CHECKING:  # `app.authz` imports the models; this module stays import-light
    from app.authz.principal import Principal
    from app.models.orgs import Org

# The system actor: a fixed, structurally valid UUIDv7 literal (not minted) so migrations can name it,
# with recognisable low bits. Not a secret: `is_system` is refused by both login paths
# (app/routes/auth.py), and a partial unique index keeps it the only such row.
SYSTEM_ACTOR_ID = UUID("01900000-0000-7000-8000-000000000001")

# What the row renders as: provenance must show an identifiable actor, not a blank or bare uuid.
SYSTEM_ACTOR_NAME = "System"
SYSTEM_ACTOR_DESCRIPTION = (
    "The non-loginable actor every machine-written row attributes to — schedulers, migrations, "
    "runners, webhooks, and any raw-SQL writer. Cannot sign in."
)
# A person's email (`identities.email`) is UNIQUE; `.invalid` (RFC 2606) can never collide with a real one.
SYSTEM_ACTOR_EMAIL = "system@cockpit.invalid"

# `OR REPLACE` so the first entity revision creates it and re-running is harmless; schema-qualified to
# avoid depending on `search_path`.
SET_UPDATED_AT_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$
"""


def set_updated_at_trigger_name(table: str) -> str:
    """The one spelling of the per-table trigger name, derived, never typed out per entity."""
    return f"trg_{table}_set_updated_at"


def set_updated_at_trigger_sql(
    table: str, columns: Sequence[str] | None = None, *, replace: bool = False
) -> str:
    """DDL attaching the shared function to one entity table.

    `table` and `columns` are fixed identifiers from the calling revision, never request data.

    `columns` narrows when the trigger fires (`BEFORE UPDATE OF a, b`: an UPDATE whose SET list mentions
    one, even as a no-op). Omit it to fire on every UPDATE. It exists for machine-stamped columns such as
    `crons.last_tick_at`; see `app/crons.py::CRON_MACHINERY_COLUMNS` before using it on a new table. The
    trigger is narrowed, not the shared function, because only the table knows its machinery columns.

    `replace=True` renders `CREATE OR REPLACE TRIGGER` (Postgres 14+) so a revision can widen the column
    list without a DROP in `upgrade()`, and with no window where an UPDATE slips through unstamped.
    """
    create = "CREATE OR REPLACE TRIGGER" if replace else "CREATE TRIGGER"
    if columns is None:
        return (
            f"{create} {set_updated_at_trigger_name(table)} "
            f"BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()"
        )
    # An empty sequence is a caller bug: it would render invalid SQL at upgrade time.
    if not columns:
        raise ValueError("columns is empty; pass None for an unrestricted trigger, never ()")
    return (
        f"{create} {set_updated_at_trigger_name(table)} "
        f"BEFORE UPDATE OF {', '.join(columns)} ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()"
    )


def set_updated_at_drop_function_sql() -> str:
    """Drop the shared function. Belongs to 0105 alone (which created it); a later entity's downgrade
    calling this would break the earlier entities' triggers.
    """
    return "DROP FUNCTION IF EXISTS public.set_updated_at()"


def new_uuid7() -> UUID:
    """Mint a UUIDv7 (RFC 9562 §5.7): 48-bit big-endian Unix ms, version 7, variant 10, 74 random bits.

    Hand-rolled because `uuid.uuid7()` needs Python 3.14: the image runs 3.14, but the node floor and CI's
    provision-python fallback are 3.12; replace it when the floor moves. The DB `uuidv7()` default mints
    most ids; this is for callers that need the id before the insert.
    """
    raw = bytearray(int(time.time() * 1000).to_bytes(6, "big") + os.urandom(10))
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant 10xx
    return UUID(bytes=bytes(raw))


# ── The wire side of the shape: how the timestamp and free-text columns leave and enter a route ─────


class StrictModel(BaseModel):
    """A wire model that 422s an undeclared key instead of silently dropping it."""

    model_config = ConfigDict(extra="forbid")


class StrippedStrictModel(StrictModel):
    """A strict wire model whose strings are also stripped — the billing bodies."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def partial_of(model: type[BaseModel], *, exclude: Sequence[str] = ()) -> type[BaseModel]:
    """The base of a Patch/Update body derived from its Create twin: every field of `model` (minus `exclude`),
    `Optional` and defaulting to None, with the Create field's constraints, description and other `Field`
    attributes carried over, on the Create's own bases and config (`extra="forbid"` stays). Subclass it under
    the Patch's own name so the OpenAPI schema keeps that name, adding the fields the Patch alone has:

        class PlanPatch(partial_of(PlanCreate, exclude=("key",))):  # type: ignore[misc,valid-type]
            active: Optional[bool] = None

    Declared `Optional[T] = None`, each field keeps the tri-state the handlers read — absent (not in
    `model_fields_set`), an explicit null, a value — exactly as the hand-written Patch declared it. What is NOT
    carried: the Create's own validators (its bases' are), and a field whose Patch type or bounds differ from
    the Create's — override that one in the subclass. A Patch with validators of its own stays hand-written."""
    unknown = set(exclude) - set(model.model_fields)
    if unknown:
        raise TypeError(f"{model.__name__} has no field {sorted(unknown)} to exclude")
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name in exclude:
            continue
        annotation: Any = info.annotation
        if info.metadata:  # the constraints (`ge`, `max_length`, ...) and validators, as `Field` stores them
            annotation = Annotated[tuple([annotation, *info.metadata])]  # type: ignore[valid-type]
        annotation = Optional[annotation]  # the metadata stays on the value's type: an explicit null never meets it
        attributes = {k: v for k, v in info._attributes_set.items() if k not in ("annotation", "default", "default_factory")}
        fields[name] = (annotation, Field(default=None, **attributes))
    base_name = f"{model.__name__}Partial"
    if model.__bases__ == (BaseModel,):  # the config is the Create's own, not inherited
        partial = create_model(base_name, __config__=model.model_config or None, __module__=model.__module__, **fields)
    else:
        partial = create_model(base_name, __base__=model.__bases__, __module__=model.__module__, **fields)
    if partial.model_config != model.model_config:
        raise TypeError(f"{model.__name__}'s config would not carry to its partial: {model.model_config}")
    return partial


def iso_or_none(value: Optional[Union[date, datetime]]) -> Optional[str]:
    """The wire projection of `created_at`/`updated_at` (or a `date` column): ISO 8601, or null when the
    column is. One spelling for every route's Out model.
    """
    return value.isoformat() if value is not None else None


class EntityOut(BaseModel):
    """The seven entity columns as an Out model serves them: a string id, `''` for no description, ISO
    timestamps, and actor display names (never the raw uuid). A plain `BaseModel`, like the Out models
    it heads, so the OpenAPI schema stays as it was. Filled by `entity_fields`."""

    id: str
    name: str
    description: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None


class RefOut(BaseModel):
    """A row as a filter option, a dropdown or a link names it: the id and the display name, nothing more.
    Subclassed per route (never aliased) so the OpenAPI schema keeps a name of its own."""

    id: str
    name: str


class UuidRefOut(BaseModel):
    """`RefOut` for the routes whose Out models carry the id as a UUID."""

    id: UUID
    name: str


def entity_fields(row, names: dict[UUID, str]) -> dict:
    """`EntityOut`'s fields off an `EntityMixin` row; `names` is `app/actors.py::actor_names_for` output."""
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description or "",
        "created_at": iso_or_none(row.created_at),
        "updated_at": iso_or_none(row.updated_at),
        "created_by": names.get(row.created_by_id),
        "updated_by": names.get(row.updated_by_id),
    }


OutModel = TypeVar("OutModel", bound=BaseModel)


def from_row(model: type[OutModel], row: object, /, **overrides: Any) -> OutModel:
    """`model` built off `row`: every field read as `row`'s same-named attribute (a column, a property, a
    dataclass field) except those in `overrides`, passed as given. The one spelling for an Out model that is
    mostly a straight copy of its row; the computed fields stay visible at the call site.

    A field `row` lacks raises `AttributeError` rather than falling to its default, which is what
    `model_validate(row, from_attributes=True)` would do: a renamed column must fail loudly, never quietly
    serve a default. A field the row carries but the wire must not is passed as an explicit override.

    A `str`/`Optional[str]` field read off a timestamp or uuid attribute is projected as the wire spells it
    (`iso_or_none`, `str`), so a call site need not say `x=iso_or_none(row.x)` for every stamp; pydantic refuses
    the raw value for a `str` field, so no other shape is possible there.
    """
    values: dict[str, Any] = {}
    projected = _str_fields(model)
    for name in model.model_fields:
        if name in overrides:
            continue
        value = getattr(row, name)
        if name in projected and isinstance(value, (date, UUID)):
            value = iso_or_none(value) if isinstance(value, date) else str(value)
        values[name] = value
    return model(**values, **overrides)


_STR_FIELDS: dict[type[BaseModel], frozenset[str]] = {}


def _str_fields(model: type[BaseModel]) -> frozenset[str]:
    """The fields typed `str` or `Optional[str]` (either spelling), read once per model: a `str | int` field
    is not one, so it keeps its raw value."""
    fields = _STR_FIELDS.get(model)
    if fields is None:
        fields = _STR_FIELDS[model] = frozenset(
            name for name, field in model.model_fields.items() if _is_str(field.annotation))
    return fields


def _is_str(annotation: Any) -> bool:
    if annotation is str:
        return True
    return get_origin(annotation) in (Union, UnionType) and set(get_args(annotation)) <= {str, type(None)}


def pick(obj: object, *names: str) -> dict[str, Any]:
    """`obj`'s `names`, each under its own name: the straight-copy half of a dict literal (an audit snapshot,
    a bulk-insert row), so the call site shows only the keys it computes. A missing name raises, as `from_row`."""
    return {name: getattr(obj, name) for name in names}


#: Postgres `integer` (int4) and `bigint` (int8): a value past these raises at the driver, not as a 422 or a 404,
#: so every id, count or cursor value a caller controls is checked against them before it is bound.
PG_INT_MIN, PG_INT_MAX = -(2**31), 2**31 - 1
PG_BIGINT_MAX = 2**63 - 1

# Edge bounds, not column widths (both are `Text`). An unbounded `name` overflows the unique-name
# btree with a `ProgramLimitExceeded`, which is not an `IntegrityError` and so 500s.
MAX_NAME = 200
MAX_DESCRIPTION = 2000

# Everything in C0 except tab/newline/return, plus DEL; see `plain_text`.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Every refusal below is a human string in `detail`, raised from the handler, never a pydantic
# validator: the console (`apiDetail` in `apps/web/src/api.ts`) surfaces `detail` only when it is a
# string, and pydantic's 422 is a list.


def plain_text(value: str, *, field: str) -> str:
    """Refuse a control character. Postgres `text` cannot hold a NUL and psycopg refuses to bind one, so an
    unfiltered NUL 500s; `str.strip()` does not remove it.
    """
    if _CONTROL_RE.search(value):
        raise HTTPException(status_code=422, detail=f"{field} contains a control character that cannot be stored")
    try:
        # Lone surrogates: accepted by `json` and pydantic but unencodable by psycopg (`UnicodeEncodeError`,
        # invisible to `_flush_or_conflict`), so they would 500.
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(
            status_code=422,
            detail=f"{field} contains an unpaired surrogate that cannot be stored",
        ) from None
    return value


def bounded_text(value: str, *, field: str, limit: int) -> str:
    """Refuse a value past `limit`, naming the length and the limit."""
    if len(value) > limit:
        raise HTTPException(status_code=422, detail=f"{field} is {len(value)} characters long; the limit is {limit}")
    return value


def validated_name(value: str, *, field: str = "name", limit: int = MAX_NAME) -> str:
    """The operator's label for the row: stripped, storable, non-blank, bounded (a nameless row renders as
    nothing). `plain_text` runs on the stripped value since `strip()` does not remove a NUL.
    """
    text = plain_text((value or "").strip(), field=field)
    if not text:
        raise HTTPException(status_code=422, detail=f"{field} cannot be blank")
    return bounded_text(text, field=field, limit=limit)


_MARKUP_RE = re.compile(r"[<>]")


def carries_markup(text: str) -> bool:
    """`text` holds `<` or `>`: no real org or person name needs them, and a name that does is a probe for a
    surface that renders it as markup."""
    return bool(_MARKUP_RE.search(text))


def clean_text(value: Any) -> str:
    """A JSON or jsonb field read as text: `value` stripped when it is a string, else "" — never a type error, so
    one odd row never fails a page (the one home of that read; `text_or_none` is its nullable form)."""
    return value.strip() if isinstance(value, str) else ""


def text_or_none(value: Any) -> Optional[str]:
    """`clean_text`, with a blank as None: a stored sentence, or nothing."""
    return clean_text(value) or None


def validated_org_name(value: str, *, field: str = "name", limit: int = MAX_NAME) -> str:
    """`validated_name` for an org, also refusing markup (`carries_markup`)."""
    text = validated_name(value, field=field, limit=limit)
    if carries_markup(text):
        raise HTTPException(status_code=422, detail=f"{field} cannot contain < or >")
    return text


def validated_description(value: Optional[str], *, limit: int = MAX_DESCRIPTION) -> str:
    """`''` for none, never null: the column is NOT NULL with a `''` default, and `None` would read as "None".
    """
    text = plain_text(value or "", field="description")
    return bounded_text(text, field="description", limit=limit)


def tenant_org_or_404(principal: Principal | None, *, detail: str) -> UUID:
    """The caller's own tenant org: 401 without a person, 404 carrying `detail` on the operator plane (it has no tenant org)."""
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if principal.org_id is None or not is_tenant_org(principal.org_id):
        raise HTTPException(status_code=404, detail=detail)
    return principal.org_id


Found = TypeVar("Found")


def or_404(found: Optional[Found], *, detail: Any) -> Found:
    """`found` when the caller's own lookup answered, else a 404 carrying `detail`: the one spelling of "looked
    up, not there" for a lookup a route performs itself (a service call, a `session.scalar`, a parsed id).
    `get_or_404` below is the same verdict over a select it runs for the caller."""
    if found is None:
        raise HTTPException(status_code=404, detail=detail)
    return found


def get_or_404(session: Session, model: type, key, *, detail: str, where: Sequence = (), lock: bool = False):
    """The `model` row whose `id` is `key` and that matches every criterion in `where` (the caller's org,
    the parent row), or a 404 carrying `detail`; `lock` takes it `FOR UPDATE`.

    `select(...)`, never `session.get`: a `get` may hit the identity map and emit nothing, bypassing the
    tenancy seam (`app/tenancy/seam.py`) and the tenancy-bypass census test.
    """
    statement = select(model).where(model.id == key, *where)
    row = session.scalars(statement.with_for_update() if lock else statement).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=detail)
    return row


def count_by(session: Session, column, keys: list, *, where: Sequence = ()) -> dict:
    """`{key: row count}` grouped on `column` for the rows whose `column` is in `keys` and that match every
    criterion in `where`, in one query (no N+1). A key with no rows is absent; callers read `.get(key, 0)`. An
    empty `keys` is `{}` without a query: the answer is already known, and `IN ()` is skipped."""
    if not keys:
        return {}
    return dict(
        session.execute(select(column, func.count()).where(column.in_(keys), *where).group_by(column)).all()
    )


def names_of(session: Session, model: type, ids: Iterable[object]) -> dict[Any, str]:
    """`{id: name}` for `model`'s rows among `ids` — the one home for the "names in one query, never N+1" read behind
    a page. A None among the ids is a row with no such link, so it is dropped; none left asks nothing. The whole
    table is `all_names_of`, spelled apart so a None that slipped in cannot widen a read."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return dict(session.execute(select(model.id, model.name).where(model.id.in_(wanted))).tuples().all())  # type: ignore[attr-defined]


def all_names_of(session: Session, model: type) -> dict[Any, str]:
    """`{id: name}` for every row of `model` the session's scope can see: no `where` of its own, so the read is
    bounded by the tenancy seam (`app/tenancy/seam.py`) alone — a catalogue (prices), or a page's own projects."""
    return dict(session.execute(select(model.id, model.name)).tuples().all())  # type: ignore[attr-defined]


def optional_uuid(value) -> Optional[UUID]:
    """`value` parsed as a uuid (any `UUID()` spelling of its `str`), or None: a malformed id, a non-string, a
    blank. The one spelling of "a uuid or nothing" for knobs, wire fields and JSON values."""
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def is_blank(value) -> bool:
    """Absent for practical purposes: None, or a string that is only whitespace."""
    return value is None or (isinstance(value, str) and not value.strip())


def readable_error(exc: BaseException) -> str:
    """An exception as a bounded reason line: its `detail` when it has one, else its text, else its type."""
    detail = getattr(exc, "detail", None)
    text = str(detail if detail is not None else exc).strip() or type(exc).__name__
    return text[:500]


def float_or_none(value) -> Optional[float]:
    """`Numeric` reads back as `Decimal`, which is not JSON; converted at the wire, NULL stays None."""
    return None if value is None else float(value)


def row_by_uuid_or_404(session: Session, model: type, raw_id: str, *, detail: str, where: Sequence = ()):
    """`get_or_404` (`where` included) for a uuid primary key given as a string. A malformed id is a 404, not a
    422, so the code does not reveal which kind of miss it was."""
    try:
        key = UUID(raw_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail=detail) from None
    return get_or_404(session, model, key, detail=detail, where=where)


def administered_org_or_404(
    session: Session, org_id: str, principal: Principal | None, *, switch: str | None = None,
    operator_too: bool = False,
) -> Org:
    """The org `org_id` (a uuid string), when the caller may administer it: an operator any org, an Org
    Owner/Admin their own; else 404 "no such org" (a malformed id and another org's id read the same).

    With `switch`, a tenant is refused 403 naming the plan feature while the org's plan leaves it out
    (`plan_terms.require_switch`); an operator never is, unless `operator_too` (off plan, nothing connects).
    """
    # Deferred: `app.plan_terms` and the models import this module.
    from app import plan_terms
    from app.models.orgs import Org

    row = row_by_uuid_or_404(session, Org, org_id, detail="no such org")
    if principal is None or not (principal.is_operator or principal.org_id == row.id):
        raise HTTPException(status_code=404, detail="no such org")
    if switch is not None and (operator_too or not principal.is_operator):
        plan_terms.require_switch(session, row.id, switch)
    return row


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, limit: Optional[int] = None) -> str:
    """`text` as a slug: lowercase, each run of non-alphanumerics one `-`, cut to `limit`, no `-` at either end.
    Empty when nothing survives (the caller names its default). The one alphabet rule; `apps/web/src/lib/slug.ts` is its twin."""
    return _SLUG_STRIP.sub("-", (text or "").lower()).strip("-")[:limit].strip("-")


def slug_taken(session: Session, model: Any, slug: str, *, where: Sequence = (), exclude: Optional[UUID] = None) -> bool:
    """Whether a row of `model` (a mapped class with `id` and `slug`) matching every criterion in `where` (the org, the scope) already
    holds `slug`; `exclude` is the row being renamed. The one spelling of the check a caller runs before a save
    whose unique constraint would otherwise answer it (`flush_or_409` turns that race into the same 409)."""
    statement = select(model.id).where(model.slug == slug, *where)
    if exclude is not None:
        statement = statement.where(model.id != exclude)
    return session.scalars(statement.limit(1)).first() is not None


def unique_slug(
    session: Session, model: Any, name: str, *, default: str, where: Sequence = (),
    exclude: Optional[UUID] = None, reserved: Sequence[str] = (), limit: int = 60, max_len: Optional[int] = None,
) -> str:
    """A slug from `name`, unique among the rows of `model` matching every criterion in `where`:
    `nightly-check`, then `nightly-check-2`, … `default` names an empty slug; `exclude` is the row being
    renamed; `reserved` are slugs no row may take (a NodeLink's `accounts`). `limit` cuts the base; a suffix
    runs past it unless `max_len` caps the whole slug (a column with a declared length), in which case the base
    is cut to make room for it."""
    base = slugify(name, limit=limit) or default
    statement = select(model.slug).where(model.slug.like(f"{base}%"), *where)
    if exclude is not None:
        statement = statement.where(model.id != exclude)
    taken = set(session.scalars(statement)) | set(reserved)
    if base not in taken:
        return base
    suffix = 2
    while True:
        tail = f"-{suffix}"
        candidate = base[: max_len - len(tail)].rstrip("-") + tail if max_len is not None else base + tail
        # A cut base is outside the prefix scan above, so that one candidate is asked for by name.
        if candidate not in taken and (
            candidate.startswith(base) or not slug_taken(session, model, candidate, where=where, exclude=exclude)
        ):
            return candidate
        suffix += 1


def unique_org_slug(
    session: Session, model: type, org_id: UUID, name: str, *, default: str,
    exclude: Optional[UUID] = None, reserved: Sequence[str] = (),
) -> str:
    """`unique_slug` among the org's rows of `model` (which has `slug` and `org_id`)."""
    return unique_slug(
        session, model, name, default=default, where=(model.org_id == org_id,), exclude=exclude, reserved=reserved,
    )


# How many slugs `add_with_org_slug` tries: each lost race costs one, so this bounds concurrent same-name creates.
SLUG_ATTEMPTS = 5


def add_with_org_slug(
    session: Session, row, name: str, *, default: str, reserved: Sequence[str] = (), attempts: int = SLUG_ATTEMPTS,
) -> None:
    """Add `row` (a new row with `org_id` and `slug`) and flush it with a `unique_org_slug` slug from `name`.

    The slug read is check-then-act: a concurrent same-name create in the org picks the same slug and the later
    flush fails on `uq_<table>_org_slug` (it waits for the winner's commit). That loss rolls back only this row's
    savepoint and allocates again, now seeing the winner's slug, so the second create gets `-2`. Anything else
    pending is flushed first, outside the savepoint, so a rollback can never take it with the row.
    """
    session.flush()
    slug_constraint = f"uq_{row.__table__.name}_org_slug"
    for attempt in range(1, attempts + 1):
        row.slug = unique_org_slug(session, type(row), row.org_id, name, default=default, reserved=reserved)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
            return
        except IntegrityError as exc:
            if constraint_name(exc) != slug_constraint or attempt == attempts:
                raise


def constraint_name(exc: Exception) -> Optional[str]:
    """The constraint Postgres names on an `IntegrityError`, or `None`. Defensive because `diag` is
    psycopg's: a missing attribute must leave the caller a re-raise, not a crash.
    """
    return getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)


@contextmanager
def conflict_as_409(
    session: Session, detail: str, *, constraints: tuple[str, ...] | frozenset[str] | None = None
) -> Iterator[None]:
    """Run the block, turning an `IntegrityError` into a 409 carrying `detail` (rolled back first).

    With `constraints`, it fails closed: any other constraint re-raises as a 500 with the real cause,
    rather than a plausible "that name is taken". Passing none asserts the unique is the only
    constraint the block can reach; a new constraint on the table must widen the call.
    """
    try:
        yield
    except IntegrityError as exc:
        session.rollback()
        if constraints is not None and constraint_name(exc) not in constraints:
            raise
        raise HTTPException(status_code=409, detail=detail) from None


@contextmanager
def refused_as(
    status: int, *errors: type[BaseException], session: Session | None = None, chain: bool = False, prefix: str = "",
) -> Iterator[None]:
    """Run the block, turning any of `errors` into an HTTP `status` carrying the error's text after `prefix`
    (the field or system it names: `"model: "`), rolled back first when `session` is given. The one spelling of
    "this domain refusal is that status" a route otherwise writes as a three-line try/except; `conflict_as_409`
    is its IntegrityError sibling. `chain` keeps the original as `__cause__` (`raise ... from exc`); off, it is
    suppressed (`from None`). `str(exc)` reaches the client: wrap only a call whose exception text is written for
    the person asking."""
    try:
        yield
    except errors as exc:
        if session is not None:
            session.rollback()
        raise HTTPException(status_code=status, detail=f"{prefix}{exc}") from (exc if chain else None)


def flush_or_409(session: Session, detail: str, *, constraints: tuple[str, ...] | frozenset[str] | None = None) -> None:
    """Flush under `conflict_as_409`."""
    with conflict_as_409(session, detail, constraints=constraints):
        session.flush()


def commit_row(
    session: Session, row: Any, *, taken: str | None = None,
    constraints: tuple[str, ...] | frozenset[str] | None = None,
) -> Any:
    """The write verbs' tail, once: flush (`taken` names the 409 a unique conflict earns, as `flush_or_409`; without
    it a conflict is the 500 it is), commit, then refresh `row` so the response carries the DB's own defaults and
    stamps. Returns `row`. A caller that must do something between the flush and the commit (a console event, a
    second row) keeps its own tail. `constraints` qualifies `taken` only: without a 409 to earn there is nothing
    to narrow, so passing them alone is refused."""
    if taken is None:
        if constraints is not None:
            raise ValueError("commit_row: constraints without taken (no 409 to narrow)")
        session.flush()
    else:
        flush_or_409(session, taken, constraints=constraints)
    session.commit()
    session.refresh(row)
    return row


def reject_cleared(changes: dict[str, Any], fields: Sequence[str], *, hint: str = "") -> None:
    """A PATCH/PUT body's explicit null on a field that has no empty value is a 422 naming the field: the one spelling
    of "cannot be cleared" (`description` can be emptied; a name, a rate, a kind cannot). `hint` follows the field
    after a semicolon ("delete the row to stop pricing this model")."""
    for field in fields:
        if field in changes and changes[field] is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be cleared" + (f"; {hint}" if hint else ""))
