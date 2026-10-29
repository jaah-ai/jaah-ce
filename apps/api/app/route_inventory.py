"""Every route an app serves, as one flat list — the one walk every route census uses.

From FastAPI 0.137, `include_router` no longer copies a router's routes into `app.routes`: it appends one
private `_IncludedRouter` node per include, so `app.routes` is a TREE and a census that filters it for
`APIRoute` sees only the handful declared on the app itself — and passes vacuously over the rest.
`fastapi.routing.iter_route_contexts` is FastAPI's own flattening; `original_route` is the `APIRoute` /
`APIWebSocketRoute` the request is dispatched to (`scope["route"]`), with its full path, because no include
here adds a prefix (every router carries its own). Before 0.137 the attribute is absent and `app.routes`
is already flat, which is what a host interpreter on an older pin still sees.

What `original_route` does NOT carry is the app's own `dependencies=` — `authz_guard` among them. Those
live on the include's effective context, which is what the request is actually run with, so a census of
what RUNS reads `dispatched_dependants`, never `route.dependant`.
"""

from __future__ import annotations

from typing import Any

from fastapi import routing as _routing


def flat_routes(app: Any) -> list[Any]:
    """`app.routes` with every included router expanded in dispatch order."""
    walk = getattr(_routing, "iter_route_contexts", None)
    if walk is None:
        return list(app.routes)
    return [context.original_route for context in walk(app.routes)]


def dispatched_dependants(app: Any) -> list[tuple[Any, Any]]:
    """`(route, dependant)` for every route, the dependant being the one a request is resolved against —
    app-level dependencies included. Same order as `flat_routes`."""
    walk = getattr(_routing, "iter_route_contexts", None)
    if walk is None:
        return [(route, getattr(route, "dependant", None)) for route in app.routes]
    return [(context.original_route, _dispatched_dependant(context)) for context in walk(app.routes)]


def _dispatched_dependant(context: Any) -> Any:
    """An HTTP route runs against the context's own `dependant`; a WebSocket route is dispatched to the
    context's `starlette_route`, an `APIWebSocketRoute` rebuilt with the include's dependencies."""
    dependant = getattr(context, "dependant", None)
    if dependant is None:
        dependant = getattr(getattr(context, "starlette_route", None), "dependant", None)
    return dependant
