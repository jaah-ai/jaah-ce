"""The task detail page's cold load in one read — `GET /api/tasks/{task_id}/page`.

`task` is `GET /api/tasks/{task_id}`'s body, and every other key is one panel's standalone GET body, as the web's
cold load asks for it (no query string). Each part runs its OWN route's handler in-process, behind that route's
own declaration (`authz.enforce_in_process`), and is serialized by that route's own response field. A panel
whose route would refuse (any `HTTPException`: 403, 404, 409) is ABSENT, so the web falls back to that panel's
GET and every refusal and empty semantic stays single-homed in the standalone route. A panel that fails any
other way is absent too, logged, and its savepoint rolled back, so the web's fallback GET surfaces that failure
in that panel alone and the next panel reads on a usable session. Nothing here decides access or shapes a body.

`trail` is not a panel: `{"project_name", "feature_name"}`, the names of the task's project and feature, so a cold
page's breadcrumb is right on first paint without waiting for the projects roster or the task tree.
Read here directly, after `task` passed its gate: a feature is seen exactly where its tasks are
(`authz/visibility.py::visible_feature_clause`), and `GET /api/features` already names a feature's project to that
same `tasks:view` holder, so neither name tells the caller more than the task does. Like a panel, it never fails
the page: an error omits it.

Its own module: `routes/attachments.py` and `routes/task_ratings.py` import `routes/tasks/`, so the composer
cannot live there without a cycle.
"""
from __future__ import annotations

import inspect
import logging
import sys
from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
from typing import Any, Callable, Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import ResponseValidationError
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from pydantic.fields import FieldInfo
from sqlalchemy import and_, event, select
from sqlalchemy.orm import Session
# Starlette's, the base of FastAPI's: a refusal raised as either is a refusal.
from starlette.exceptions import HTTPException

from app.auth import adopt_user, require_user
from app.authz.declarations import declaration_of, feature
from app.authz.enforce import enforce_in_process
from app.authz.guard import authz_guard
from app.authz.principal import AsyncCaller, current_caller, current_principal
from app.db_async import DbDep, get_db
from app.offload import run_cpu
from app.models.projects import Feature, Project
from app.models.tasks import Task
from app.task_lookup import page_memo
from app.route_inventory import dispatched_dependants
from app import edition_features
from app.routes import attachments, task_comments, task_control_messages, task_ratings
from app.routes.tasks import activity, create, dependencies, read

if edition_features.enabled("compliance"):
    from app.routes import compliance_manifests
if edition_features.enabled("live_cast"):
    from app.routes import live_cast

log = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

# The panels, in the page's reading order: response key → the standalone route's handler.
_PANELS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("activity", activity.get_task_activity),
    ("artifacts", attachments.list_artifacts),
    ("run_metrics", activity.get_task_run_metrics),
    ("rating", task_ratings.get_task_rating),
    ("comments", task_comments.list_comments),
    ("body_versions", create.list_body_versions),
    ("dependencies", dependencies.get_task_dependencies),
    # An edition without compliance has no manifest route, so no such panel.
    *((("compliance_manifest", compliance_manifests.get_compliance_manifest),)
      if edition_features.enabled("compliance") else ()),
    ("control_messages", task_control_messages.list_control_messages),
    *((("live_cast", live_cast.get_task_live_cast),) if edition_features.enabled("live_cast") else ()),
)

# The dependencies this route resolves, by the callable a handler names in its `Depends(...)`: each part gets
# the very objects its own GET would (FastAPI caches a dependency per request, so it is one call either way).
# `get_db` is the route's session: an `on_session` handler's body takes its sync facade first. `current_caller`
# is the three as one (`AsyncCaller`), the route's own, its actor adopted into the session.
_RESOLVED_DEPENDENCIES = {
    require_user: "user", current_principal: "principal", get_db: "session", current_caller: "caller",
}
# Every dependency a composed route may RUN: the resolved ones, and the global guard, which this route runs
# too and whose per-route decision `authz.enforce_in_process` replays. Anything else would be skipped in-process.
_RUN_DEPENDENCIES = frozenset((*_RESOLVED_DEPENDENCIES, authz_guard))


def _body_of(handler: Callable[..., Any]) -> Callable[..., Any]:
    """What a part runs: an `on_session` handler's sync body (`session_body`, `def name(session, ...)`), an async
    handler's private one (`_<name>`, the handler being `await db.run_sync(_<name>, ...)`), else the sync handler
    itself."""
    body = getattr(handler, "session_body", None)
    if body is not None:
        return body
    if not inspect.iscoroutinefunction(inspect.unwrap(handler)):
        return handler
    return getattr(sys.modules[handler.__module__], f"_{handler.__name__}")


def _annotated_dependency(param: inspect.Parameter) -> Any:
    """The `Depends(...)` an `Annotated[..., Depends(...)]` parameter carries (`DbDep`), else None."""
    for marker in getattr(param.annotation, "__metadata__", ()):
        if isinstance(marker, DependsParam):
            return marker
    return None


def _arguments_plan(handler: Callable[..., Any]) -> dict[str, str]:
    """`handler`'s parameters → which resolved value each receives. Built at import and fails it: a handler
    that grows a dependency this route does not resolve, or a required parameter, must not be called with a
    guess. A defaulted plain parameter (`list_comments`'s `limit`) keeps its default, as the web sends none; a
    `Query(...)`/`Header(...)`/`Cookie(...)`/`Body(...)` default is refused, since the marker itself would be
    passed in where FastAPI would have resolved a value."""
    plan: dict[str, str] = {}
    for name, param in inspect.signature(handler, eval_str=True).parameters.items():
        if isinstance(param.default, FieldInfo):
            raise TypeError(f"{handler.__name__}.{name} defaults to a FastAPI parameter marker, which "
                            "GET /api/tasks/{task_id}/page cannot resolve — give it a plain default")
        depends = param.default if isinstance(param.default, DependsParam) else _annotated_dependency(param)
        if depends is not None:
            source = _RESOLVED_DEPENDENCIES.get(depends.dependency)
            if source is None:
                raise TypeError(f"{handler.__name__}.{name} depends on {depends.dependency!r}, which "
                                "GET /api/tasks/{task_id}/page does not resolve — resolve it there too")
            plan[name] = source
        elif name in ("task_id", "request"):
            plan[name] = name
        elif param.default is inspect.Parameter.empty:
            raise TypeError(f"{handler.__name__}.{name} is required and GET /api/tasks/{{task_id}}/page "
                            "cannot supply it")
    return plan


def _body_call(handler: Callable[..., Any], plan: dict[str, str]) -> Callable[[dict[str, Any]], Any]:
    """How a part runs `handler`'s body over the resolved values: a session body takes the session
    first and every other parameter by name, a defaulted plain one (`list_comments`'s `limit`) at its default, as
    the web sends none; a sync handler takes its plan as keywords."""
    body = _body_of(handler)
    if body is handler:
        return lambda resolved: handler(**{name: resolved[source] for name, source in plan.items()})
    defaults = {name: param.default for name, param in inspect.signature(handler).parameters.items()
                if name not in plan and param.default is not inspect.Parameter.empty}
    named = {name: source for name, source in plan.items() if source != "session"}
    return lambda resolved: body(
        resolved["session"], **defaults, **{name: resolved[source] for name, source in named.items()}
    )


_PLANS = {handler: _arguments_plan(handler) for handler in (read.get_task, *(h for _, h in _PANELS))}
_CALLS = {handler: _body_call(handler, plan) for handler, plan in _PLANS.items()}


def _check_dependencies(route: APIRoute, dependant: Any) -> None:
    """Refuses `route` unless every dependency its request RUNS is one in-process composition honours: the
    handler's signature is all `_arguments_plan` sees, so a route-level `dependencies=[Depends(...)]` (its own
    or an include's) would otherwise be silently skipped. `dependant` is the one the request is resolved
    against (`route_inventory.dispatched_dependants`), app- and include-level dependencies included."""
    calls = [d.dependency for d in route.dependencies] + [d.call for d in dependant.dependencies]
    extra = [call for call in calls if call not in _RUN_DEPENDENCIES]
    if extra:
        raise TypeError(f"{route.path} runs {extra!r}, which GET /api/tasks/{{task_id}}/page would skip "
                        "in-process — resolve it there too, or drop the part")


@lru_cache(maxsize=8)
def _composed_routes(app: Any) -> dict[Callable[..., Any], APIRoute]:
    """Each composed handler → its `APIRoute` in `app`, every one checked by `_check_dependencies` — built on
    the first page read and failing it, never a part run without its route's dependencies."""
    composed = set(_PLANS)
    routes = {route.endpoint: (route, dependant) for route, dependant in dispatched_dependants(app)
              if isinstance(route, APIRoute) and route.endpoint in composed}
    missing = [handler.__name__ for handler in composed if handler not in routes]
    if missing:
        raise TypeError(f"no route serves {missing!r}")
    for route, dependant in routes.values():
        _check_dependencies(route, dependant)
    return {handler: route for handler, (route, _dependant) in routes.items()}


def _serialized(route: APIRoute, content: Any) -> Any:
    """`content` as `route` itself answers it: its response field's validation, then its serialization with the
    route's own include/exclude settings — `fastapi.routing.serialize_response`, which is async."""
    field = route.response_field
    if field is None:
        raise TypeError(f"{route.path} declares no response model to serialize its body with")
    value, errors = field.validate(content, {}, loc=("response",))
    if errors:
        raise ResponseValidationError(errors=errors, body=content)
    return field.serialize(
        value,
        include=route.response_model_include,
        exclude=route.response_model_exclude,
        by_alias=route.response_model_by_alias,
        exclude_unset=route.response_model_exclude_unset,
        exclude_defaults=route.response_model_exclude_defaults,
        exclude_none=route.response_model_exclude_none,
    )


def _trail(session: Session, task_id: int) -> dict[str, str | None]:
    """The breadcrumb's names: the task's project and feature, one query; `feature_name` is null for a
    task filed in the project itself. Pinned to the task's org, though the composite keys already hold it there."""
    row = session.execute(
        select(Project.name, Feature.name)
        .select_from(Task)
        .join(Project, and_(Project.id == Task.project_id, Project.org_id == Task.org_id))
        .outerjoin(Feature, and_(Feature.id == Task.feature_id, Feature.org_id == Task.org_id))
        .where(Task.id == task_id)
    ).first()
    return {"project_name": None if row is None else row[0], "feature_name": None if row is None else row[1]}


@router.get("/api/tasks/{task_id}/page", response_model=None)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
async def get_task_page(
    db: DbDep,
    task_id: int,
    request: Request,
    caller: AsyncCaller = Depends(current_caller),
) -> JSONResponse:
    """The task detail page's cold load: `{"task": <GET /api/tasks/{id}>, "<panel>": <its GET>, ...}`.

    Keys: `task`, `trail`, `activity`, `artifacts`, `run_metrics`, `rating`, `comments`, `body_versions`,
    `dependencies`, `compliance_manifest` (only with compliance), `control_messages`, `live_cast` (only with Live
    Cast). `trail` is `{"project_name", "feature_name"}` (`_trail`), absent if its read fails. `task` refuses as
    its own GET does (this route carries `get_task`'s declaration, and `get_task`'s refusal propagates). A panel is ABSENT
    whenever its own GET would refuse this caller — its declaration (`authz.enforce_in_process`) or its
    handler raising `HTTPException` — so the reader falls back to that GET; absent never means empty. A panel
    failing any other way is absent too (logged, its savepoint rolled back), where it used to fail that panel
    alone; `task` is never caught.

    One verdict per distinct declaration, since the decision reads only the connection and the declaration.
    This route's own is seeded admitted — the guard just decided it — and `get_task` and eight panels share it,
    so only `live_cast` (`@org_admin`) is decided here."""
    page = await db.run_sync(_get_task_page, task_id, request, caller)
    # Every part is already its route's JSON-mode serialization (`_serialized`), so the dict goes out as it is:
    # handed back as a dict, FastAPI re-walked it through `jsonable_encoder` — over half the handler's time on a
    # long-history task, on the GIL. Encoded off the loop: a long-history page is megabytes.
    return await run_cpu(JSONResponse, page)


def _get_task_page(session: Session, task_id: int, request: Request, caller: AsyncCaller) -> dict[str, Any]:
    """`get_task_page`'s body, over the route's async session (`run_sync`): the page, each part serialized."""
    caller = replace(caller, actor=adopt_user(session, caller.actor))
    resolved = {
        "task_id": task_id, "request": request, "user": caller.actor, "principal": caller.principal,
        "session": session, "caller": caller,
    }
    routes = _composed_routes(request.app)
    verdicts: dict[Any, HTTPException | None] = {declaration_of(get_task_page): None}

    def run(handler: Callable[..., Any]) -> Any:
        declaration = declaration_of(handler)
        if declaration not in verdicts:
            try:
                enforce_in_process(request, handler)
                verdicts[declaration] = None
            except HTTPException as refusal:
                verdicts[declaration] = refusal
        if verdicts[declaration] is not None:
            raise verdicts[declaration]
        content = _CALLS[handler](resolved)
        return _serialized(routes[handler], content)

    # One row read per page, not per panel: `get_task_or_404` serves every part the row `task` loaded.
    with page_memo(session):
        page: dict[str, Any] = {"task": run(read.get_task)}
        with _isolation(session) as isolated:
            # The breadcrumb's names: after `task`, so a refusal never reaches it; isolated like a panel.
            try:
                with isolated():
                    page["trail"] = _trail(session, task_id)
            except Exception:
                log.exception("task page: the trail failed for task %s; omitted", task_id)
            for key, handler in _PANELS:
                try:
                    with isolated():
                        page[key] = run(handler)
                except HTTPException:
                    continue
                except Exception:
                    log.exception("task page: the %s panel failed for task %s; omitted", key, task_id)
    return page


@contextmanager
def _isolation(session: Session) -> Iterator[Callable[[], Any]]:
    """`isolated()` blocks that each fail alone: a part's error rolls back to a SAVEPOINT taken before it, so it
    cannot leave the shared session aborted for the next part. The SAVEPOINT is SHARED by consecutive parts that
    write nothing — rolling back to it then undoes only the failing part, exactly what one SAVEPOINT per part
    did — and a fresh one is taken after a part that wrote (`after_flush`, or a non-SELECT through the session),
    after a failure, or once a part's own commit ended it. Two statements per page instead of two per part."""
    state: dict[str, Any] = {"savepoint": None, "wrote": False}

    def flushed(*_args: Any) -> None:
        state["wrote"] = True

    def executed(orm_execute_state: Any) -> None:
        if not orm_execute_state.is_select:
            state["wrote"] = True

    @contextmanager
    def isolated() -> Iterator[None]:
        savepoint = state["savepoint"]
        if savepoint is None or not savepoint.is_active:
            savepoint = state["savepoint"] = session.begin_nested()
            state["wrote"] = False
        try:
            yield
            # A part that only staged rows (`session.add`, no flush) wrote too: flushing here, inside its own
            # savepoint, marks it a writer (`after_flush`), so a later part's failure never rolls its rows back.
            session.flush()
        except BaseException:
            if savepoint.is_active:
                savepoint.rollback()
            state["savepoint"] = None
            raise
        if state["wrote"] and savepoint.is_active:
            savepoint.commit()
            state["savepoint"] = None

    event.listen(session, "after_flush", flushed)
    event.listen(session, "do_orm_execute", executed)
    try:
        yield isolated
    finally:
        event.remove(session, "after_flush", flushed)
        event.remove(session, "do_orm_execute", executed)
        savepoint = state["savepoint"]
        if savepoint is not None and savepoint.is_active:
            savepoint.commit()
