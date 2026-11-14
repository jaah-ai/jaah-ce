"""Features — hierarchy level 3, read and written (`app/features.py` owns the layer's rules).

Access is the EXISTING task keys at the feature's project, with no model of its own (D9): whoever may see
a project's tasks (`tasks:view`) sees its features; whoever may create tasks there creates features; whoever may edit its tasks edits and deletes them. A feature a caller cannot see
is simply absent from a list and 404s by id (`authz.FEATURE_SPEC`); so does a deleted one.

One pinned response shape, `FeatureOut` (the console's rail, pickers and feature page consume it). Every
write emits a `feature` console event, which the SSE relay filters per subscriber like a task's
(`authz.OBJECT_TYPES`).

Filing a TASK into a feature is a task write (`PUT /api/tasks/{id}` with `feature_id`), not a route here:
`routes/tasks/update.py::_apply_feature_filing`. Deleting never touches a task's state: it moves the feature's
draft tasks to the project and leaves every other task in it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import actors
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller, principal_for_user
from app.authz.visibility import has_project_key, scope_key_clause, visible_feature_clause
from app.clock import utcnow
from app.db_async import on_session
from app.entity_shape import UuidRefOut, commit_row, from_row, get_or_404, partial_of
from app.events import emit_console_event
from app.identities import display_name, label_expr
from app.features import (
    DELETED_FEATURE_MOVES_STATE,
    FEATURE_DESCRIPTION_MAX,
    FEATURE_NAME_MAX,
)
from app.models.identity import AdminUser
from app.models.projects import FEATURE_ICONS, Feature, Project
from app.models.tasks import Task
from app.routes.tags import _renumber, _validated_order
from app.tenancy.ids import is_tenant_org
from app.user_activity import record_write

# One detail for every miss, so a foreign feature is indistinguishable from a missing one.
_NO_SUCH_FEATURE = "unknown feature"

router = APIRouter(prefix="/api/features", tags=["features"])

TASKS_EDIT_KEY = permission_key("tasks", "edit")


class UserRef(UuidRefOut):
    """A person as a feature names one. `name` is the provenance name (`actors.actor_names`), so a tenant
    reader sees a withheld placeholder for an operator-side actor, never their name."""


class FeatureOut(BaseModel):
    """One feature as every consumer reads it. THE PINNED SHAPE — the console's feature surfaces bind to
    these names."""

    id: UUID
    name: str
    description: str
    project_id: int
    project_name: str
    lead: UserRef
    """The feature's lead, labelled "Owner" in the console (`models.Feature.lead_id` says why the column
    is not called `owner_id`)."""
    icon: Optional[str]
    """One of `models.FEATURE_ICONS` (`"<shape>-<colour>"`), or null for "No icon"."""
    show_progress: bool
    """Whether the task rail draws this feature's completion ring (`models.Feature.show_progress`)."""
    created_at: datetime
    created_by: UserRef
    can_edit: bool
    """Whether THIS caller may edit, delete and file into it: `tasks:edit` on its project."""


class FeatureCreate(BaseModel):
    """`POST /api/features`. `project_id` is authorized `tasks:create` there, as for a task. `lead_id`
    absent is the creator."""

    project_id: int
    name: str
    description: str = ""
    lead_id: Optional[UUID] = None


class FeatureUpdate(partial_of(FeatureCreate, exclude=("project_id",))):  # type: ignore[misc,valid-type]
    """`PUT /api/features/{id}` — a PATCH in all but the verb: an absent key is "leave it". The project is
    not settable: a feature never moves (D4)."""

    icon: Optional[str] = None
    """Tri-state, read through `model_fields_set`: absent leaves it, null clears it ("No icon"), a token
    sets it. A token outside `models.FEATURE_ICONS` is a 422."""
    show_progress: Optional[bool] = None
    """Absent leaves it; null is a 422 (the column is a plain on/off)."""


class FeatureOrderIn(BaseModel):
    """`PUT /api/features/order` — every live (not deleted) feature of one project, in the intended rail order."""

    project_id: int
    feature_ids: list[str]


def _validated_name(value: Optional[str]) -> str:
    """A feature's name, stripped, or 422. The DDL check holds the same bound for raw writers."""
    name = (value or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="a feature needs a name")
    if len(name) > FEATURE_NAME_MAX:
        raise HTTPException(status_code=422, detail=f"a feature name is at most {FEATURE_NAME_MAX} characters")
    return name


def _validated_description(value: Optional[str]) -> str:
    """A feature's description, or 422. `None` (the `PUT` clear) becomes `''` for the NOT NULL column."""
    description = (value or "").strip()
    if len(description) > FEATURE_DESCRIPTION_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"a feature description is at most {FEATURE_DESCRIPTION_MAX} characters",
        )
    return description


def _may_lead(session: Session, user: AdminUser, project: Project, actor: AdminUser) -> bool:
    """Is `user` a lead-candidate of `project`: a person of its org, not the system actor, holding
    `tasks:edit` there? The one home of the rule, shared by the candidate list and the lead validation, so
    the picker offers exactly what a write accepts.

    Judged with the candidate's superadmin flag dropped when a tenant actor names someone else, as
    `authz.named_user_reaches_project` does, so the answer is not an `is_superadmin` oracle."""
    if user.is_system or user.org_id != project.org_id:
        return False
    probe = principal_for_user(session, user)
    if is_tenant_org(actor.org_id) and user.id != actor.id and probe.is_superadmin:
        probe = replace(probe, is_superadmin=False)
    return has_project_key(probe, TASKS_EDIT_KEY, project.id)


# One sentence for every refused lead, whatever the reason, so the 422 names nobody and is no oracle for
# another org's directory.
_LEAD_REFUSED = "lead_id must name someone who can edit tasks in this project"


def _validated_lead(session: Session, lead_id: UUID, project: Project, actor: AdminUser) -> UUID:
    """`lead_id`, if it names one of `project`'s lead-candidates, else 422 (`_LEAD_REFUSED`)."""
    user = session.get(AdminUser, lead_id)
    if user is None or not _may_lead(session, user, project, actor):
        raise HTTPException(status_code=422, detail=_LEAD_REFUSED)
    return user.id


def _default_lead(actor: AdminUser, project: Project) -> UUID:
    """The creator, when the body names no lead. Refused rather than stored when the creator is not a
    person of the project's org (an operator writing into a tenant's project): the database's same-org key
    would refuse the row as a 500 otherwise."""
    if actor.is_system or actor.org_id != project.org_id:
        raise HTTPException(
            status_code=422,
            detail="lead_id is required: you are not a member of this project's organization",
        )
    return actor.id


def _live_feature(session: Session, feature_id: UUID, *, lock: bool = False) -> Feature:
    """The feature by id, or the one 404 for missing, foreign and deleted alike."""
    feature_row = get_or_404(session, Feature, feature_id, detail=_NO_SUCH_FEATURE, lock=lock)
    if feature_row.deleted_at is not None:
        raise HTTPException(status_code=404, detail=_NO_SUCH_FEATURE)
    return feature_row


def _outs(session: Session, principal: Principal | None, rows: list[tuple[Feature, Project]]) -> list[FeatureOut]:
    """The pinned shape for a page of rows, with every actor name resolved in one query."""
    names = actors.actor_names(
        session,
        [
            actor_id
            for feature_row, _project in rows
            for actor_id in (feature_row.lead_id, feature_row.created_by_id)
        ],
    )

    def ref(actor_id: Optional[UUID]) -> Optional[UserRef]:
        return None if actor_id is None else UserRef(id=actor_id, name=names.get(actor_id, ""))

    return [
        from_row(
            FeatureOut, feature_row,
            project_id=project.id,
            project_name=project.name,
            lead=ref(feature_row.lead_id),
            created_by=ref(feature_row.created_by_id),
            can_edit=has_project_key(principal, TASKS_EDIT_KEY, project.id),
        )
        for feature_row, project in rows
    ]


def _out(session: Session, principal: Principal | None, feature_row: Feature) -> FeatureOut:
    """One feature's pinned shape. An unscoped `session.get`: the guard already admitted the feature, and
    its project is its own."""
    return _outs(session, principal, [(feature_row, session.get(Project, feature_row.project_id))])[0]


@router.get("", response_model=list[FeatureOut])
@feature("tasks", "view", collection=True)
@on_session
def list_features(
    session: Session,
    project_id: Optional[int] = None,
    editable: bool = False,
    caller: AsyncCaller = Depends(current_caller),
) -> list[FeatureOut]:
    """Every feature this caller may see, narrowed by the filters. Invisible rows are simply absent.

    A deleted feature is never listed: the pickers and the rail must never offer one. `editable=true` keeps only the
    features whose project grants the caller `tasks:edit` (the "Move to feature" picker)."""
    stmt = (
        select(Feature, Project)
        .join(Project, Project.id == Feature.project_id)
        .where(visible_feature_clause(caller.principal), Feature.deleted_at.is_(None))
    )
    if project_id is not None:
        stmt = stmt.where(Feature.project_id == project_id)
    if editable:
        stmt = stmt.where(scope_key_clause(caller.principal, "project", TASKS_EDIT_KEY, Feature.project_id))
    rows = session.execute(stmt.order_by(Project.name, Feature.name, Feature.id)).all()
    return _outs(session, caller.principal, [tuple(row) for row in rows])


@router.get("/{feature_id}", response_model=FeatureOut)
@feature("tasks", "view", object_type="feature", object_params=("feature_id",))
@on_session
def get_feature(
    session: Session,
    feature_id: UUID,
    caller: AsyncCaller = Depends(current_caller),
) -> FeatureOut:
    """One live feature; a deleted one 404s like a missing one."""
    return _out(session, caller.principal, _live_feature(session, feature_id))


@router.post("", response_model=FeatureOut, status_code=201)
@feature("tasks", "create")
@on_session
def create_feature(
    session: Session,
    body: FeatureCreate,
    caller: AsyncCaller = Depends(current_caller),
) -> FeatureOut:
    """Add a feature to one project (AC-4.5: `tasks:create` there, as for a task). `project_id` arrives in
    the body, so the route guard cannot check it: "does not exist" and "not yours" are one 422, or the
    route becomes an existence oracle."""
    project = session.get(Project, body.project_id)
    if project is None or not has_project_key(caller.principal, permission_key("tasks", "create"), body.project_id):
        raise HTTPException(status_code=422, detail="project not found")
    name = _validated_name(body.name)
    description = _validated_description(body.description)
    lead_id = (
        _validated_lead(session, body.lead_id, project, caller.actor)
        if body.lead_id is not None
        else _default_lead(caller.actor, project)
    )
    feature_row = Feature(
        name=name,
        description=description,
        project_id=project.id,
        lead_id=lead_id,
        # Last in its project's rail order. Racy, accepted as `tags._next_position`: a tie falls to `name`.
        position=int(
            session.scalar(select(func.max(Feature.position)).where(Feature.project_id == project.id)) or 0
        ) + 1,
    )
    stamp_actor(feature_row, caller.actor, created=True)
    session.add(feature_row)
    session.flush()
    emit_console_event(session, entity="feature", id=feature_row.id, action="created")
    commit_row(session, feature_row)
    return _out(session, caller.principal, feature_row)


# Declared above `PUT /{feature_id}` on purpose: FastAPI would otherwise match `order` as that route's
# parameter and this endpoint would be unreachable.
@router.put("/order", status_code=204)
@feature("tasks", "edit")
@on_session
def reorder_features(
    session: Session,
    body: FeatureOrderIn,
    caller: AsyncCaller = Depends(current_caller),
) -> Response:
    """The rail's drag-to-reorder: `position` 1..n over the project's complete live feature list, in one
    transaction; deleted features keep theirs. `tasks:edit` there, as renaming one; the project arrives in
    the body, so "does not exist" and "not yours" are one 422 (`create_feature`'s reason). Idempotent."""
    if session.get(Project, body.project_id) is None or not has_project_key(
        caller.principal, TASKS_EDIT_KEY, body.project_id
    ):
        raise HTTPException(status_code=422, detail="project not found")
    rows = list(
        session.scalars(
            select(Feature)
            .where(Feature.project_id == body.project_id, Feature.deleted_at.is_(None))
            .order_by(Feature.position, Feature.name, Feature.id)
            .with_for_update()
        )
    )
    order = _validated_order(body.feature_ids, [row.id for row in rows], noun="feature")
    _renumber(rows, order)
    # One event per feature that moved, so every other open rail re-reads its tree.
    for row in rows:
        if session.is_modified(row):
            emit_console_event(session, entity="feature", id=row.id, action="updated")
    session.commit()
    return Response(status_code=204)


@router.put("/{feature_id}", response_model=FeatureOut)
@feature("tasks", "edit", object_type="feature", object_params=("feature_id",))
@on_session
def update_feature(
    session: Session,
    feature_id: UUID,
    body: FeatureUpdate,
    caller: AsyncCaller = Depends(current_caller),
) -> FeatureOut:
    """Rename, describe and/or re-lead one live feature (AC-4.4). A null `name` or `lead_id` is refused; a null
    `description` or `icon` clears it."""
    feature_row = _live_feature(session, feature_id)
    sent = body.model_dump(exclude_unset=True)
    # Validated before any assignment, so a 422 never leaves a partial write staged.
    if sent.get("icon") is not None and sent["icon"] not in FEATURE_ICONS:
        raise HTTPException(status_code=422, detail="unknown icon")
    if "show_progress" in sent and sent["show_progress"] is None:
        raise HTTPException(status_code=422, detail="show_progress must be true or false")
    if "name" in sent:
        feature_row.name = _validated_name(sent["name"])
    if "description" in sent:
        feature_row.description = _validated_description(sent["description"])
    if "lead_id" in sent:
        if sent["lead_id"] is None:
            raise HTTPException(status_code=422, detail="a feature needs a lead")
        if sent["lead_id"] != feature_row.lead_id:
            project = session.get(Project, feature_row.project_id)
            feature_row.lead_id = _validated_lead(session, sent["lead_id"], project, caller.actor)
    if "icon" in sent:
        feature_row.icon = sent["icon"]
    if "show_progress" in sent:
        feature_row.show_progress = sent["show_progress"]
    stamp_actor(feature_row, caller.actor)
    emit_console_event(session, entity="feature", id=feature_row.id, action="updated")
    return _out(session, caller.principal, commit_row(session, feature_row))


@router.delete("/{feature_id}", status_code=204)
@feature("tasks", "edit", object_type="feature", object_params=("feature_id",))
@on_session
def delete_feature(session: Session, feature_id: UUID, actor: AdminUser = Depends(require_user)) -> Response:
    """Delete one feature: it leaves the rail, the pickers and filing, and its row stays so the pipeline's later
    stages and past releases still name it. Its draft tasks move to the project; every other task keeps it. No
    task's state changes. A deleted feature 404s, so a second delete is a 404."""
    # `FOR UPDATE`: a write serializes against a concurrent filing (which reads the row `FOR SHARE`).
    feature_row = _live_feature(session, feature_id, lock=True)
    name = feature_row.name
    project_id = feature_row.project_id
    feature_row.deleted_at = utcnow()
    feature_row.deleted_by_id = actor.id
    stamp_actor(feature_row, actor)
    # A draft top-level row moves with its whole subtree (a subtree is filed as one, `_apply_feature_filing`); a
    # started epic keeps its draft stage tasks with it.
    moved = (
        select(Task.id)
        .where(Task.feature_id == feature_row.id, Task.parent_task_id.is_(None), Task.state == DELETED_FEATURE_MOVES_STATE)
        .cte("moved", recursive=True)
    )
    moved = moved.union(select(Task.id).where(Task.parent_task_id == moved.c.id))
    session.execute(
        update(Task)
        .where(Task.id.in_(select(moved.c.id)))
        .values(feature_id=None, **actors.task_actor_values(session))
    )
    emit_console_event(session, entity="feature", id=feature_row.id, action="deleted")
    # The one hand-written audit here (`tests/test_activity_kinds.py` shape 1): the 204's captured request holds only
    # a uuid, so the name is recorded explicitly.
    record_write(
        session, actor, "feature_deleted", f"Deleted feature {name}",
        data={"feature_id": feature_id, "project_id": project_id, "name": name},
    )
    session.commit()
    return Response(status_code=204)


@router.get("/{feature_id}/lead-candidates", response_model=list[UserRef])
@feature("tasks", "edit", object_type="feature", object_params=("feature_id",))
@on_session
def list_lead_candidates(session: Session, feature_id: UUID, actor: AdminUser = Depends(require_user)) -> list[UserRef]:
    """The people this feature's lead may be: non-system users of its org holding `tasks:edit` on its
    project (`_may_lead`, the rule the lead validation applies), ordered by name. Read through
    `app/access.py`'s resolution, never a second copy of it."""
    feature_row = _live_feature(session, feature_id)
    project = session.get(Project, feature_row.project_id)
    users = session.scalars(
        select(AdminUser)
        .where(AdminUser.org_id == project.org_id, AdminUser.is_system.is_(False))
        .order_by(label_expr(), AdminUser.id)
    ).all()
    return [
        UserRef(id=user.id, name=display_name(user))
        for user in users
        if _may_lead(session, user, project, actor)
    ]
