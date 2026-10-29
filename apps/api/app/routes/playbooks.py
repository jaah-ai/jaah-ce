"""Playbooks — `/api/playbooks`, the list and editor behind the console's Playbooks sandbox.

A playbook is an ordered list of stages (`app/playbook_vocabulary.py` holds the vocabulary). Two tables
answer one list: the four built-ins (`built_in_playbooks`, one global copy each, read-only — D1) and the
caller org's own copies (`playbooks`). `origin` on the wire is which table a row came from.

A playbook runs only through `app/playbook_engine.py`, the one bridge to the task runtime; no task, dispatch
or pipeline module may name this one (`tests/test_playbook_isolation.py`). Each row also carries the task
form's picker fields (`playbook_engine.picker_fields`).

Access: every org member reads, and an operator or an Org Owner/Admin of the caller's own org mutates
(`_may_edit`, 403 otherwise) — ON EITHER PLANE. Deliberately not `@org_admin`: that gate gives an org role
nothing on the operator plane, because there it would reach the fleet's users, nodes and crons, and a
playbook is none of those — one org's rows, run only as a snapshot a task takes of them. So every route is
`@always(Tenancy.ORG_SCOPED)`; the guard binds nothing, `_member_session` binds the caller's own org, and
EVERY handler also filters `Playbook.org_id` to it — another org's copy is a 404 for everyone. The one
exception is READ-ONLY and the operator's alone (`_reader_session`): `GET ?all_orgs=true` lists every org's
copies and `GET /{id}` opens any of them, never editable or duplicable — every mutation stays own-org.

Plan: every route is a 403 (`playbook_engine.OFF_DETAIL`) while the caller org's billing plan has Playbooks
off (`playbook_engine.enabled`), checked where the caller's own-org session opens (`_member_session`). The
operator's fleet reads above (and `/vocabulary`, which they render with) answer regardless: they read other
orgs' copies, not the operator org's own feature.

Validation is `app/playbook_validation.py`'s, run identically on create, update, `/validate` and every
read. The Pydantic models here check JSON shape only; an error issue refuses a save with
`422 {"detail": {"issues": [...]}}`, a warning never does (D7).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated, Any, Iterator, Literal, Optional, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import act_as
from app import playbook_engine
from app import playbook_vocabulary as v
from app.actors import stamp_actor
from app.access import ORG_ADMIN, ORG_OWNER
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.principal import Principal, current_principal
from app.db import SessionLocal
from app.entity_shape import constraint_name, get_or_404, iso_or_none
from app.models.identity import AdminUser
from app.models.tasks import BuiltInPlaybook, Playbook
from app.playbook_built_ins import BUILT_INS
from app.playbook_prompt import StageNotRenderable, compose_stage
from app.playbook_validation import (
    ERROR,
    MAX_EXTRA_INSTRUCTIONS,
    MAX_NAME,
    MAX_STAGES,
    Issue,
    derive,
    has_errors,
    name_taken_issue,
    validate,
)
from app.tenancy import seam
from app.tenancy.labels import OrgRef, org_ref_of, org_refs

router = APIRouter(prefix="/api/playbooks", tags=["playbooks"])

BUILT_IN, TENANT = "built_in", "tenant"
READ_ONLY_DETAIL = "Built-in playbooks are read-only. Duplicate it to edit a copy."
NOT_FOUND_DETAIL = "playbook not found"
_NAME_UNIQUE = "uq_playbooks_org_name"
_DUPLICATE_ATTEMPTS = 5
_SEED_ORDER = {UUID(built_in.id): position for position, built_in in enumerate(BUILT_INS)}


# ── The wire shape (JSON shape only — membership is `validate()`'s) ──────────────────────────────────

# Hard bounds on the body, well past every human limit `validate()` words for the editor (a name past
# `MAX_NAME` is an Issue, not this): they exist so an oversized body is refused while parsing, before any
# rule runs over it.
_Token = Annotated[str, StringConstraints(max_length=200)]
_Text = Annotated[str, StringConstraints(max_length=MAX_EXTRA_INSTRUCTIONS * 2)]
_TOKENS = 100
_BODY_STAGES = MAX_STAGES * 4


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpertPinned(_Shape):
    kind: Literal["pinned"]
    expert: _Token


class ExpertChosen(_Shape):
    kind: Literal["chosen_at_start"]
    default: _Token
    allowed: Annotated[list[_Token], Field(max_length=_TOKENS)]


class CreatedAtStart(_Shape):
    kind: Literal["at_start"]


class CreatedBy(_Shape):
    kind: Literal["by"]
    stage: _Token


class DependsNone(_Shape):
    kind: Literal["none"]


class DependsAllEarlier(_Shape):
    kind: Literal["all_earlier"]


class DependsStages(_Shape):
    kind: Literal["stages"]
    keys: Annotated[list[_Token], Field(max_length=_TOKENS)]


class StageIn(_Shape):
    key: _Token
    label: _Text
    expert: Annotated[Union[ExpertPinned, ExpertChosen], Field(discriminator="kind")]
    # Null iff the expert is chosen at start: the deliverable then follows the expert (D3).
    deliverable: Optional[_Token] = None
    seniority: _Token
    created: Annotated[Union[CreatedAtStart, CreatedBy], Field(discriminator="kind")]
    runs: _Token
    depends_on: Annotated[
        Union[DependsNone, DependsAllEarlier, DependsStages], Field(discriminator="kind")
    ]
    interviews: _Token
    extra_instructions: _Text = ""
    # Null = the deliverable's whole filing set; a list may only narrow it.
    may_file: Optional[Annotated[list[_Token], Field(max_length=_TOKENS)]] = None


class PlaybookIn(_Shape):
    name: _Text
    description: _Text = ""
    entry: _Token
    full_auto_default: bool
    default_seniority: _Token
    stages: Annotated[list[StageIn], Field(max_length=_BODY_STAGES)]


# ── Rendering ────────────────────────────────────────────────────────────────────────────────────────


def _as_input(body: PlaybookIn) -> dict[str, Any]:
    """The stored/validated form: the wire shape as plain JSON, name and description trimmed."""
    pb = body.model_dump(mode="json")
    pb["name"] = pb["name"].strip()
    pb["description"] = pb["description"].strip()
    for stage in pb["stages"]:
        stage["label"] = stage["label"].strip()
    return pb


def _row_input(row: Union[Playbook, BuiltInPlaybook]) -> dict[str, Any]:
    return {
        "name": row.name,
        "description": row.description,
        "entry": row.entry,
        "full_auto_default": row.full_auto_default,
        "default_seniority": row.default_seniority,
        "stages": list(row.stages),
    }


def _stages_out(pb: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**stage, "derived": derived} for stage, derived in zip(pb["stages"], derive(pb))]


def _out(
    row: Union[Playbook, BuiltInPlaybook], *, may_edit: bool, org: Optional[OrgRef] = None
) -> dict[str, Any]:
    """`may_edit` is the caller's `_may_edit` for a row in their own org — pass False for another org's
    (the operator's read-only view). `org` is a tenant row's label; a built-in belongs to no org."""
    pb = _row_input(row)
    built_in = isinstance(row, BuiltInPlaybook)
    return {
        **pb,
        "id": str(row.id),
        "origin": BUILT_IN if built_in else TENANT,
        "org": None if org is None else org.model_dump(mode="json"),
        "editable": not built_in and may_edit,
        # Apart from `editable`: a built-in is never editable, yet the caller may still duplicate it.
        "may_duplicate": may_edit,
        "stages": _stages_out(pb),
        "issues": [issue.as_dict() for issue in validate(pb)],
        **playbook_engine.picker_fields(pb),
        "created_at": iso_or_none(row.created_at),
        "updated_at": iso_or_none(row.updated_at),
    }


def _may_edit(principal: Optional[Principal]) -> bool:
    """An operator, or an Org Owner/Admin of the caller's own org on either plane (the module header)."""
    return principal is not None and (principal.is_operator or principal.org_role in (ORG_OWNER, ORG_ADMIN))


def _refuse(issues) -> HTTPException:
    return HTTPException(status_code=422, detail={"issues": [issue.as_dict() for issue in issues]})


# ── Lookups (every one filtered to the caller's own org) ─────────────────────────────────────────────


def _built_in(session: Session, playbook_id: UUID) -> Optional[BuiltInPlaybook]:
    return session.scalars(select(BuiltInPlaybook).where(BuiltInPlaybook.id == playbook_id)).one_or_none()


def _own(session: Session, org_id: Any, playbook_id: UUID) -> Optional[Playbook]:
    return session.scalars(
        select(Playbook).where(Playbook.id == playbook_id, Playbook.org_id == org_id)
    ).one_or_none()


def _own_or_refuse(session: Session, user: AdminUser, playbook_id: UUID) -> Playbook:
    """The caller org's copy, or 409 for a built-in, or 404."""
    if _built_in(session, playbook_id) is not None:
        raise HTTPException(status_code=409, detail=READ_ONLY_DETAIL)
    return get_or_404(
        session, Playbook, playbook_id, detail=NOT_FOUND_DETAIL, where=(Playbook.org_id == user.org_id,)
    )


def _name_taken(session: Session, org_id: Any, name: str, *, except_id: Optional[UUID] = None) -> bool:
    query = select(Playbook.id).where(Playbook.org_id == org_id, Playbook.name == name)
    if except_id is not None:
        query = query.where(Playbook.id != except_id)
    return session.scalars(query.limit(1)).first() is not None


def _checked(session: Session, user: AdminUser, body: PlaybookIn, *, except_id: Optional[UUID] = None):
    pb = _as_input(body)
    issues = validate(pb)
    if pb["name"] and _name_taken(session, user.org_id, pb["name"], except_id=except_id):
        issues.insert(0, name_taken_issue(pb["name"]))
    if has_errors(issues):
        raise _refuse(issues)
    return pb


def _commit_or_name_taken(session: Session, name: str) -> None:
    """Commit; a concurrent insert of the same name answers the same 422 issue, never a 500."""
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if constraint_name(exc) != _NAME_UNIQUE:
            raise
        raise _refuse([name_taken_issue(name)]) from None


def _apply(row: Playbook, pb: dict[str, Any]) -> None:
    row.name = pb["name"]
    row.description = pb["description"]
    row.entry = pb["entry"]
    row.full_auto_default = pb["full_auto_default"]
    row.default_seniority = pb["default_seniority"]
    row.stages = pb["stages"]


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────────


@contextmanager
def _member_session(principal: Optional[Principal]) -> Iterator[tuple[Session, Any]]:
    """A session bound to the caller's own org, and that org — 401 without a signed-in person. The guard
    bound nothing (`ALWAYS`), so there is no decided scope for the region to conflict with."""
    if principal is None or principal.org_id is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    with seam.bound_to(principal.org_id), SessionLocal() as session:
        if not playbook_engine.enabled(session, principal.org_id):
            raise HTTPException(status_code=403, detail=playbook_engine.OFF_DETAIL)
        yield session, principal.org_id


@contextmanager
def _reader_session(principal: Optional[Principal], *, fleet: bool) -> Iterator[tuple[Session, Any]]:
    """`_member_session`, or — for an operator asking `fleet` — a fleet-wide session, so the read reaches
    every org's copies. Reads only: no mutation enters it. Yields the caller's own org either way."""
    if not (fleet and principal is not None and principal.is_operator):
        with _member_session(principal) as pair:
            yield pair
        return
    # The same 401 `_member_session` answers: "own" is meaningless without an org of one's own.
    if principal.org_id is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    # The operator's org filter narrows the fleet read; set only on an operator's GET.
    with seam.operator_scope(), seam.request_org_filter_applied(), SessionLocal() as session:
        yield session, principal.org_id


def _labelled(session: Session, rows: list[Playbook]) -> dict[Any, OrgRef]:
    return org_refs(session, [row.org_id for row in rows])


@contextmanager
def _editor_session(principal: Optional[Principal]) -> Iterator[tuple[Session, AdminUser]]:
    """`_member_session` for a mutation: 403 unless `_may_edit`, and the caller's own row to stamp."""
    with _member_session(principal) as (session, _org_id):
        if not _may_edit(principal):
            raise HTTPException(status_code=403, detail="forbidden")
        # A filtered read, not `session.get`: the seam's org predicate applies to it.
        user = act_as.own_row(session, principal)
        if user is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        yield session, user


def _signed_in(principal: Optional[Principal], *, operator_read: bool = False) -> None:
    """401 without a signed-in person, 403 while their org's plan has Playbooks off (`_member_session`) —
    unless `operator_read` and the caller is an operator."""
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if operator_read and principal.is_operator:
        return
    with _member_session(principal):
        pass


@router.get("")
@always(Tenancy.ORG_SCOPED)
def list_playbooks(
    principal: Optional[Principal] = Depends(current_principal),
    all_orgs: bool = Query(
        False,
        description="An operator's ask for every org's copies (the console's Playbooks list); ignored for "
        "anyone else, and off by default so the task form offers only the caller org's own.",
    ),
) -> dict[str, Any]:
    """The built-ins in seed order, then the caller org's copies by name — or, for an operator asking
    `all_orgs`, every org's copies by name, another org's read-only."""
    may_edit = _may_edit(principal)
    with _reader_session(principal, fleet=all_orgs) as (session, org_id):
        built_ins = sorted(
            session.scalars(select(BuiltInPlaybook)).all(),
            key=lambda row: _SEED_ORDER.get(row.id, len(_SEED_ORDER)),
        )
        query = select(Playbook).order_by(Playbook.name, Playbook.id)
        if not (all_orgs and principal.is_operator):
            query = query.where(Playbook.org_id == org_id)
        copies = list(session.scalars(query).all())
        refs = _labelled(session, copies)
        return {
            "playbooks": [
                *(_out(row, may_edit=may_edit) for row in built_ins),
                *(
                    _out(row, may_edit=may_edit and row.org_id == org_id, org=org_ref_of(refs, row.org_id))
                    for row in copies
                ),
            ]
        }


@router.get("/vocabulary")
@always(Tenancy.ORG_SCOPED)
def vocabulary(principal: Optional[Principal] = Depends(current_principal)) -> dict[str, Any]:
    """Every closed set with its human label, so the console renders no raw token."""
    _signed_in(principal, operator_read=True)
    return {
        "experts": [{"key": e.key, "label": e.label} for e in v.EXPERT_RECORDS],
        "deliverables": [
            {
                "key": d.key,
                "label": d.label,
                "mode": d.mode,
                "ends_when": d.ends_when,
                "may_file": [{"key": item, "label": v.FILING_LABELS[item]} for item in d.may_file],
            }
            for d in v.DELIVERABLE_RECORDS
        ],
        "seniorities": [
            {"key": key, "label": v.SENIORITY_LABELS[key], "tier": tier}
            for key, tier in v.SENIORITY_TIERS.items()
        ],
        "labels": v.labels(),
    }


@router.post("/validate")
@always(Tenancy.ORG_SCOPED)
def validate_playbook(
    body: PlaybookIn, principal: Optional[Principal] = Depends(current_principal)
) -> dict[str, Any]:
    """The issues and derived facts of an unsaved playbook — always 200, never a 422 for an issue.
    Name uniqueness is checked on save only (an edit would otherwise collide with its own name)."""
    _signed_in(principal)
    pb = _as_input(body)
    return {"issues": [issue.as_dict() for issue in validate(pb)], "stages": _stages_out(pb)}


class PreviewIn(_Shape):
    playbook: PlaybookIn
    stage_key: _Token
    # A chosen-at-start stage's pick among its allowed experts; None = its default.
    expert: Optional[_Token] = None
    # None = the playbook's own `full_auto_default`.
    full_auto: Optional[bool] = None


@router.post("/preview-prompt")
@always(Tenancy.ORG_SCOPED)
def preview_prompt(
    body: PreviewIn, principal: Optional[Principal] = Depends(current_principal)
) -> dict[str, Any]:
    """One stage's rendered dispatch prompt against the sample fixture (`app/playbook_prompt.py`), for an
    unsaved edit as readily as a saved one. An invalid playbook or an unrenderable stage is a 422."""
    _signed_in(principal)
    pb = _as_input(body.playbook)
    issues = validate(pb)
    if has_errors(issues):
        raise _refuse(issues)
    try:
        return compose_stage(pb, body.stage_key, expert=body.expert, full_auto=body.full_auto)
    except StageNotRenderable as exc:
        raise _refuse([Issue(body.stage_key, None, "stage_key", ERROR, str(exc))]) from None


@router.get("/{playbook_id}")
@always(Tenancy.ORG_SCOPED)
def get_playbook(
    playbook_id: UUID, principal: Optional[Principal] = Depends(current_principal)
) -> dict[str, Any]:
    """A built-in, or the caller org's copy — or, for an operator, any org's copy, read-only."""
    with _reader_session(principal, fleet=True) as (session, org_id):
        row = _built_in(session, playbook_id) or session.scalars(
            select(Playbook).where(Playbook.id == playbook_id)
        ).one_or_none()
        # The handler's own org check is the guarantee (the seam narrows a non-operator's read too, but this
        # line must not be dropped as redundant): only an operator opens another org's copy.
        if row is None or (isinstance(row, Playbook) and row.org_id != org_id and not principal.is_operator):
            raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
        own = not isinstance(row, Playbook) or row.org_id == org_id
        org = org_ref_of(_labelled(session, [row]), row.org_id) if isinstance(row, Playbook) else None
        return _out(row, may_edit=own and _may_edit(principal), org=org)


# ── Mutations ────────────────────────────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
@always(Tenancy.ORG_SCOPED)
def create_playbook(
    body: PlaybookIn,
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    with _editor_session(principal) as (session, user):
        pb = _checked(session, user, body)
        row = Playbook(org_id=user.org_id)
        _apply(row, pb)
        stamp_actor(row, user, created=True)
        session.add(row)
        _commit_or_name_taken(session, pb["name"])
        session.refresh(row)
        return _out(row, may_edit=True)


@router.put("/{playbook_id}")
@always(Tenancy.ORG_SCOPED)
def update_playbook(
    playbook_id: UUID,
    body: PlaybookIn,
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    """A full replace; the stage order sent is the order stored."""
    with _editor_session(principal) as (session, user):
        row = _own_or_refuse(session, user, playbook_id)
        pb = _checked(session, user, body, except_id=row.id)
        _apply(row, pb)
        stamp_actor(row, user)
        _commit_or_name_taken(session, pb["name"])
        session.refresh(row)
        return _out(row, may_edit=True)


def _copy_name(session: Session, org_id: Any, name: str) -> str:
    """The first free of "<name> copy", "<name> copy 2", … in the caller's org, within `MAX_NAME`."""
    taken = set(session.scalars(select(Playbook.name).where(Playbook.org_id == org_id)).all())
    number = 1
    while True:
        suffix = " copy" if number == 1 else f" copy {number}"
        candidate = name[: MAX_NAME - len(suffix)].rstrip() + suffix
        if candidate not in taken:
            return candidate
        number += 1


@router.post("/{playbook_id}/duplicate", status_code=201)
@always(Tenancy.ORG_SCOPED)
def duplicate_playbook(
    playbook_id: UUID, principal: Optional[Principal] = Depends(current_principal)
) -> dict[str, Any]:
    """A new, editable copy in the caller's org of a built-in or of one of the org's own copies."""
    with _editor_session(principal) as (session, user):
        source = _built_in(session, playbook_id) or _own(session, user.org_id, playbook_id)
        if source is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
        pb, source_name = _row_input(source), source.name
        # A concurrent duplicate can take the same free name first; the caller named nothing, so pick the
        # next free one rather than answer "choose a different name".
        for _attempt in range(_DUPLICATE_ATTEMPTS):
            pb["name"] = _copy_name(session, user.org_id, source_name)
            row = Playbook(org_id=user.org_id)
            _apply(row, pb)
            stamp_actor(row, user, created=True)
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if constraint_name(exc) != _NAME_UNIQUE:
                    raise
                continue
            session.refresh(row)
            return _out(row, may_edit=True)
    raise HTTPException(status_code=409, detail="Could not find a free name for the copy. Try again.")


@router.delete("/{playbook_id}", status_code=204)
@always(Tenancy.ORG_SCOPED)
def delete_playbook(
    playbook_id: UUID,
    principal: Optional[Principal] = Depends(current_principal),
) -> Response:
    """A hard delete (D-DEL); a built-in is a 409."""
    with _editor_session(principal) as (session, user):
        session.delete(_own_or_refuse(session, user, playbook_id))
        session.commit()
    # Not `-> None`: FastAPI would derive a response model and assert on a bodiless 204.
    return Response(status_code=204)
