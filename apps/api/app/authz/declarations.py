"""Route declarations: the `Declaration` a handler carries and the decorators that attach it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from app.authz.catalog import (
    ACTIONS,
    AUTHOR_DRAFT_ACTIONS,
    AUTHOR_DRAFT_PARAM,
    Access,
    CREDENTIALS_FEATURE,
    FEATURES,
    PERMISSION_KEYS,
    Tenancy,
)
from app.authz.visibility import OBJECT_TYPES


@dataclass(frozen=True)
class Declaration:
    """The authorization contract of ONE route, attached to its handler as `__authz__`."""

    access: Access
    """WHO may call it."""

    tenancy: Tenancy
    """WHAT the call may see. NO DEFAULT — see `Tenancy`. Second, and therefore ahead of every
    defaulted field, because a dataclass cannot carry a mandatory field after an optional one. That
    ordering is also why `__post_init__` below checks the TYPE rather than trusting a positional
    argument to have landed in the slot its author meant."""
    feature: str | None = None
    action: str | None = None
    keys: tuple[str, ...] = ()
    """FEATURE_ANY ONLY: the rendered `<feature>:<action>` keys, ANY ONE of which admits the caller.

    Rendered keys rather than a `(feature, action)` pair list because that is what `Principal.has`
    takes and what a reader of the decorator line has to reason about; `declare` validates every one
    of them against `PERMISSION_KEYS` at import time, so a typo is an ImportError and not a route
    nobody can reach."""

    object_type: str | None = None
    """`"project"` or `"task"` — which entity `object_params` name ids of."""
    object_params: tuple[str, ...] = ()
    """PATH params holding object ids. The GUARD enforces ALL of them before the handler runs."""
    collection: bool = False
    """The handler returns a collection and MUST build its query via `scoped_task_select` /
    `scoped_project_select`. The guard cannot enforce this — a collection has no id to check — so
    the marker exists to make an unscoped collection handler visible to review, not to gate it."""
    lookup: bool = False
    """The object is identified by QUERY params, not path params, so the HANDLER enforces access
    itself (via `require_object_access`) once it has resolved the object."""
    agent_allowed: bool = False
    """A dual-principal route (today: the `require_user_or_agent` carve-out paths). A cookie-less
    agent-token caller is permitted; a session caller still needs the declared access."""
    author_draft: bool = False
    """TASK WRITE ROUTES ONLY: `AUTHOR_DRAFT_KEY` ALSO admits, when this route's `AUTHOR_DRAFT_PARAM`
    names a still-`draft` task the caller authored. See that section's header for the rule.

    IT ADMITS ONE ROW UNDER ONE KEY AND NOTHING ELSE. The declared key is checked first — at the
    route-level floor, and again ON THIS ROW'S PROJECT — and only a caller the declared key
    does not carry through both is probed against `authored_draft_task_clause`. Since a task is not a
    grantable object the object check is a key test too — `editable_task_clause`
    asks for a task-write key on the project — so the same predicate stands in on BOTH halves, and
    only on the caller's own still-`draft` top-level row (`author_draft_admits`). The flag can never
    reach a row its holder did not file: `editable_task_clause` itself grows no author arm.

    AND IT IS TRIED PER ROW, NOT ONLY WHEN THE FLOOR REFUSED, because the alternative is a permission
    that TAKES something away: a caller holding `tasks:edit` on another project clears the floor, so a
    row check scoped to the declared key alone would refuse them their own draft here — while a caller
    holding no `tasks:edit` at all sails through. `_enforce` carries the ordering that keeps the extra
    probe off the happy path.

    DECLARED PER ROUTE, NEVER INFERRED, because the line it draws is the whole point. Every route that
    CHANGES a draft carries it — the title/body edit, priority, tags, the dependency edges, attachments,
    and the draft's own delete. Every route that RUNS one does NOT: `/start`, `/force-start`,
    `/reset`, `/stop`, `/complete` and the three answer verbs are `tasks:edit`, exactly as before. A route added later gets the floor —
    absent means not widened, which is the safe default and the one a reviewer can check by grepping."""

    own_credential: bool = False
    """CREDENTIAL ROUTES ONLY: when the declared key is refused, ANY authenticated principal is ALSO
    admitted — and the handler then narrows every row it touches to the credentials this caller
    CREATED and holds a direct grant on (`owned_credential_clause`). Bring-your-own-credential: a
    Jaah user stores their own third-party secret without holding a `credentials:*` key, which is
    still the key that reaches ANOTHER user's row.

    IT WIDENS WHICH KEY OPENS THE DOOR, NEVER WHICH ROWS ARE BEHIND IT — the same line `author_draft`
    draws, and for the same reason. THE DIFFERENCE IS WHERE THE NARROWING LIVES: `author_draft` names
    ONE task by path param, so the guard can probe it and `_enforce_object_params` still runs the
    unchanged object check. A credential route has no such probe to make — this arm admits EVERY
    session — so the ONLY enforcement is the handler's own scoped select. `routes/credentials/`
    routes all of them through `scoped_credential_select_for`/`_scoped_row_or_404` for exactly that
    reason: an unscoped handler carrying this flag would be a real hole, and the flag alone would not
    say so.

    DECLARED PER ROUTE, NEVER INFERRED. Absent means not widened — the safe default, greppable by a
    reviewer. `POST /api/credentials/test` deliberately does NOT carry it and states why at its own
    declaration: it dials an operator-supplied host FROM the control plane, which is a decision about
    the CP's outbound reach rather than about whose secret a user may store."""

    def __post_init__(self) -> None:
        """Membership checked on the dataclass itself, not only in `declare`.

        A legacy positional call would land strings in the wrong slots, and kernel tests construct
        `Declaration` directly. `isinstance`, not `==`: `Tenancy` mixes in `str`, so a bare string
        would compare equal yet match no `is` branch."""
        if not isinstance(self.tenancy, Tenancy):
            raise TypeError(
                f"tenancy must be a Tenancy member, got {self.tenancy!r}; every route declares which "
                f"tenancy plane it runs on — one of {[t.value for t in Tenancy]}"
            )


_F = TypeVar("_F", bound=Callable[..., Any])


def declare(access: Access, **kwargs: Any) -> Callable[[_F], _F]:
    """Attach a `Declaration` to a route handler.

    Mutates and returns the same function, so it is order-insensitive relative to `@router.get(...)` (a
    wrapper below the router decorator would silently do nothing)."""

    if not isinstance(access, Access):
        # `Access` mixes in `str`, so a bare string compares equal; membership is enforced here.
        raise TypeError(f"access must be an Access member, got {access!r}")
    if "tenancy" not in kwargs:
        # Refused at import with a message about the route's plane, not the dataclass's argument name.
        raise TypeError(
            f"{access.value} declarations must name their tenancy plane: tenancy=Tenancy.<"
            + "|".join(t.name for t in Tenancy)
            + ">. There is no default, deliberately — see Tenancy's docstring."
        )
    declaration = Declaration(access=access, **kwargs)
    if declaration.access in (Access.FEATURE, Access.FEATURE_ANY):
        # A feature route always resolves a principal with an org (`None` = unbound = `false()`), so
        # only `ORG_SCOPED` is legitimate: `OPERATOR` would give a grantable key a fleet-wide read.
        if declaration.tenancy not in (Tenancy.ORG_SCOPED, Tenancy.CALLER_PLANE):
            raise ValueError(
                f"a {declaration.access.value} route is reached only by resolving a principal, so its "
                f"tenancy is that principal's org; got {declaration.tenancy.value}. A route that must "
                "read fleet-wide is @superadmin_only(Tenancy.OPERATOR) or @infra(Tenancy.OPERATOR), "
                "never a grantable key over the whole fleet"
            )
    if declaration.tenancy is Tenancy.CALLER_PLANE and declaration.access in (Access.INFRA, Access.ALWAYS):
        # The plane is decided by the resolved principal; these classes never resolve one here.
        raise ValueError(
            f"a {declaration.access.value} route resolves no session principal, so it has no caller to "
            "decide its plane by; CALLER_PLANE is for @feature, @feature_any, @org_admin and "
            "@superadmin_only"
        )
    if declaration.access is Access.INFRA and declaration.tenancy is Tenancy.BOOTSTRAP:
        # No INFRA route runs before a scope can be bound: `/api/agent/*` goes through
        # `routes/agent/auth.py::_check_token` (asserted by
        # `test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`); the node
        # WebSockets bind nothing but fail closed as `ORG_SCOPED`. A "not today", re-openable
        # deliberately. Refused at import because `authz_guard` never binds on INFRA, so nothing else
        # could observe a wrong term.
        raise ValueError(
            "no INFRA route in this tree runs before a scope can be bound, so none may claim "
            "BOOTSTRAP: every /api/agent/* route is reached through _check_token, which binds, and the "
            "off-prefix ones bind nothing but still resolve a node identity. Use Tenancy.ORG_SCOPED "
            "(the calling node's own org — an unbound org-scoped read fails CLOSED) or "
            "Tenancy.OPERATOR (fleet-wide by design). If a genuinely unbound INFRA route now exists, "
            "re-opening BOOTSTRAP here is a deliberate decision, not a workaround"
        )
    if declaration.access is Access.ORG_ADMIN and declaration.tenancy not in (
        Tenancy.ORG_SCOPED, Tenancy.CALLER_PLANE
    ):
        # An Org Owner/Admin is bound to their own org; only an operator widens, and CALLER_PLANE says so.
        raise ValueError(
            f"an org_admin route reads the caller's own org (CALLER_PLANE widens it for an operator); "
            f"got {declaration.tenancy.value}. A fleet-wide route is @superadmin_only(Tenancy.OPERATOR)"
        )
    if declaration.access is Access.SUPERADMIN and declaration.tenancy is Tenancy.BOOTSTRAP:
        # A superadmin route resolves a principal first, so it cannot be BOOTSTRAP; refused at import
        # rather than as an inexplicable runtime 403.
        raise ValueError(
            "a superadmin route resolves a principal before it decides, so it cannot be BOOTSTRAP; "
            "use Tenancy.ORG_SCOPED (the caller's own org) or Tenancy.OPERATOR (fleet-wide by design)"
        )
    if declaration.access is Access.FEATURE:
        if declaration.feature not in FEATURES:
            raise ValueError(f"unknown feature {declaration.feature!r}; expected one of {FEATURES}")
        if declaration.action not in ACTIONS:
            raise ValueError(f"unknown action {declaration.action!r}; expected one of {ACTIONS}")
    elif declaration.feature is not None or declaration.action is not None:
        raise ValueError(f"{declaration.access.value} declarations carry no feature/action")
    if declaration.access is Access.FEATURE_ANY:
        # Two or more: a one-key disjunction reads as one while enforcing one; use `Access.FEATURE`.
        if len(declaration.keys) < 2:
            raise ValueError(
                "feature_any names a DISJUNCTION and needs at least two keys; for one key the "
                "declaration is @feature(<feature>, <action>), which is also the only shape that can "
                "carry object scoping"
            )
        if len(set(declaration.keys)) != len(declaration.keys):
            raise ValueError(f"feature_any keys must be distinct, got {list(declaration.keys)}")
        # Against the catalog at import: a misspelt key is one nobody holds.
        unknown = sorted(set(declaration.keys) - PERMISSION_KEYS)
        if unknown:
            raise ValueError(f"unknown permission keys {unknown}; expected members of PERMISSION_KEYS")
    elif declaration.keys:
        raise ValueError(
            f"{declaration.access.value} declarations carry no keys; only feature_any reads them"
        )
    if declaration.object_type is not None and declaration.object_type not in OBJECT_TYPES:
        raise ValueError(
            f"unknown object_type {declaration.object_type!r}; expected one of {OBJECT_TYPES}"
        )
    if (declaration.object_params or declaration.lookup) and declaration.object_type is None:
        raise ValueError("object-scoped declarations must name an object_type")
    # An `object_type` with no enforcing mechanism is inert and would read as object-scoped while
    # checking only the feature key.
    if declaration.object_type is not None and not (
        declaration.object_params or declaration.lookup or declaration.collection
    ):
        raise ValueError(
            "object_type without object_params/lookup/collection enforces nothing: name the path "
            "params the guard should check, set lookup=True, or mark it collection=True"
        )
    # Reject what the guard will not enforce (both fail open): object scoping applies only in the
    # FEATURE branch, and `agent_allowed` is consulted before the SUPERADMIN branch.
    if declaration.access is not Access.FEATURE:
        if (
            declaration.object_type
            or declaration.object_params
            or declaration.lookup
            # The ALWAYS branch admits cookie-less callers, and `scoped_task_select(None)` is unfiltered.
            or declaration.collection
        ):
            raise ValueError(
                f"{declaration.access.value} declarations cannot be object-scoped; only "
                "Access.FEATURE enforces object_type/object_params/lookup/collection"
            )
        if declaration.agent_allowed:
            raise ValueError(
                f"agent_allowed is meaningless on a {declaration.access.value} declaration; "
                "use Access.INFRA for the agent/node principal surface"
            )
    if declaration.author_draft:
        # Every precondition the fallback assumes, asserted at import: each fails silently at request
        # time as an indistinguishable denial.
        if declaration.access is not Access.FEATURE:
            raise ValueError(
                f"author_draft conditionally widens ONE declared key; a {declaration.access.value} "
                "declaration names none"
            )
        if declaration.object_type != "task":
            raise ValueError(
                f"author_draft is a rule about a TASK; got object_type={declaration.object_type!r}"
            )
        if AUTHOR_DRAFT_PARAM not in declaration.object_params:
            raise ValueError(
                f"author_draft reads the {AUTHOR_DRAFT_PARAM!r} path param to name the draft; got "
                f"object_params={list(declaration.object_params)}"
            )
        if declaration.action not in AUTHOR_DRAFT_ACTIONS:
            raise ValueError(
                f"author_draft widens a task WRITE, not {declaration.action!r}; expected one of "
                f"{sorted(AUTHOR_DRAFT_ACTIONS)}"
            )
        # Flags never reasoned about alongside the fallback: `agent_allowed` admits earlier, `lookup` is a
        # second identity, `collection` has no id to bound the draft to.
        for flag in ("collection", "lookup", "agent_allowed"):
            if getattr(declaration, flag):
                raise ValueError(
                    f"author_draft names ONE task by path param; it cannot combine with {flag}"
                )
    if declaration.own_credential:
        # Asserted at import, and narrower than `author_draft`: this arm admits every session, so only
        # the credentials handler's `scoped_credential_select_for` stands between it and the table.
        if declaration.access is not Access.FEATURE:
            raise ValueError(
                f"own_credential conditionally widens ONE declared key; a {declaration.access.value} "
                "declaration names none"
            )
        if declaration.feature != CREDENTIALS_FEATURE:
            raise ValueError(
                f"own_credential is a rule about a CREDENTIAL; got feature={declaration.feature!r}, "
                f"expected {CREDENTIALS_FEATURE!r}"
            )
        # The shapes `author_draft` refuses, for a stronger reason: `agent_allowed` would hand a
        # cookie-less node token (`None`, allow-all) `visible_credential_clause(None)` over the whole
        # table, and `collection`/`lookup` opt out of the narrowing this flag depends on.
        if declaration.agent_allowed:
            raise ValueError(
                "own_credential admits every authenticated session and agent_allowed admits a "
                "cookie-less one, whose None principal reads as allow-all in every clause here; the "
                "pair would serve the whole credentials table to a node token"
            )
        if declaration.collection or declaration.lookup:
            raise ValueError(
                "own_credential's ONLY enforcement is the handler's scoped select, so it may not ride "
                "a declaration that opts out of the guard's own id check "
                f"(collection={declaration.collection}, lookup={declaration.lookup})"
            )

    def _attach(fn: _F) -> _F:
        fn.__authz__ = declaration  # type: ignore[attr-defined]
        return fn

    return _attach


def feature(feature_name: str, action: str, **kwargs: Any) -> Callable[[_F], _F]:
    """Sugar for the ordinary case: `@feature("tasks", "view")`, or with object scoping,
    `@feature("tasks", "edit", object_type="task", object_params=("task_id",))`.

    Supplies `Tenancy.ORG_SCOPED`: the one access class whose verdict is forced by the class itself, and
    `declare` pins it (census: `test_authz_registry.py`)."""
    kwargs.setdefault("tenancy", Tenancy.ORG_SCOPED)
    return declare(Access.FEATURE, feature=feature_name, action=action, **kwargs)


def feature_any(*keys: str, **kwargs: Any) -> Callable[[_F], _F]:
    """Sugar for the disjunction: `@feature_any("worktrees:edit", "system:create")`.

    Takes rendered keys, validated against the catalog. The guard is only a floor on such a route (see
    `Access.FEATURE_ANY`). Supplies `Tenancy.ORG_SCOPED` as `feature` does."""
    kwargs.setdefault("tenancy", Tenancy.ORG_SCOPED)
    return declare(Access.FEATURE_ANY, keys=tuple(keys), **kwargs)


# Superadmin, infra and always are factories the call site must invoke with a tenancy
# (`@superadmin_only(Tenancy.OPERATOR)`); they supply no default because their verdict is a judgement.
# A leftover bare `@superadmin_only` passes the handler as `tenancy` and fails at import.


def superadmin_only(tenancy: Tenancy, **kwargs: Any) -> Callable[[_F], _F]:
    """`@superadmin_only(Tenancy.OPERATOR)` — the fleet's own administration; or
    `@superadmin_only(Tenancy.ORG_SCOPED)` where the subject is rows that belong to one org."""
    return declare(Access.SUPERADMIN, tenancy=tenancy, **kwargs)


def org_admin(tenancy: Tenancy, **kwargs: Any) -> Callable[[_F], _F]:
    """`@org_admin(Tenancy.CALLER_PLANE)` — tenant administration: an Org Owner/Admin in their own org,
    an operator fleet-wide (G4). See `Access.ORG_ADMIN`."""
    return declare(Access.ORG_ADMIN, tenancy=tenancy, **kwargs)


def infra(tenancy: Tenancy, **kwargs: Any) -> Callable[[_F], _F]:
    """`@infra(Tenancy.OPERATOR)` for the fleet-wide agent arms, `@infra(Tenancy.ORG_SCOPED)` where the
    node's own org is what the call may see.

    `BOOTSTRAP` is refused by `declare`. `/api/node-bootstrap` is `ORG_SCOPED` (`_check_token` binds the
    node's org); the off-prefix node WebSockets bind nothing and fail closed as `ORG_SCOPED`."""
    return declare(Access.INFRA, tenancy=tenancy, **kwargs)


def always(tenancy: Tenancy, **kwargs: Any) -> Callable[[_F], _F]:
    """`@always(Tenancy.BOOTSTRAP)` for the login surface and health, `@always(Tenancy.ORG_SCOPED)` for
    a route that authorizes per subject inside its own handler (see `Tenancy`)."""
    return declare(Access.ALWAYS, tenancy=tenancy, **kwargs)


#: The attribute `session_only` sets on a handler (`is_session_only` reads it).
_SESSION_ONLY_ATTR = "__session_only__"


def session_only(fn: _F) -> _F:
    """Refuse an API key (`app/api_keys.py`) on this route: 403 `api_key_not_allowed`, checked by `authz_guard`
    before any other decision, so an `@always` route is covered too. For the routes a standing bearer credential
    must never reach — the caller's own sign-in (password, MFA, sessions, org switch), key management itself, and
    every route that mints another credential or admits a member. A marker beside the declaration, not a field of
    it: it narrows WHO may present, never WHAT the route decides. Order-insensitive like `declare`.
    `tests/test_api_keys.py` holds the census of the routes that must carry it."""
    setattr(fn, _SESSION_ONLY_ATTR, True)
    return fn


def is_session_only(endpoint: Any) -> bool:
    return getattr(endpoint, _SESSION_ONLY_ATTR, False) is True


def declaration_of(endpoint: Any) -> Declaration | None:
    """The `Declaration` a handler carries, or None when it carries none."""
    found = getattr(endpoint, "__authz__", None)
    return found if isinstance(found, Declaration) else None
