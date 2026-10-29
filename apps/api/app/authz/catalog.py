"""The permission vocabulary: features, actions, the key catalog, and the `Access`/`Tenancy` route tiers."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app import access
from app.access_catalog import ROLES_BY_SLUG, expand as expand_role


# ── The key vocabulary — ONE home, and this is it ─────────────────────────────────────────────────
# {feature} x {action} as "<feature>:<action>", read by every producer and validator; order is the
# matrix's render order. Org-level keys (`accounts` … `users`) are held org-wide by an org role and
# confined by `app/tenancy/seam.py`; the few a project role carries are `access_catalog.PROJECT_ROLE_ORG_KEYS`.
# THE TIER KEY BECOMES THE DOOR: those subjects were re-keyed off `system:*` onto one plain `@feature`
# each (no `system:*` disjunction): `test_authz_policy.py::_carried_keys`.
# Fleet capability stays `@superadmin_only` (`_OPERATOR_PLANE_ALLOWLIST`).
FEATURES = (
    "finance", "system", "credentials", "projects", "tasks", "worktrees", "compliance",
    "accounts", "audit", "crons", "nodes", "sessions", "tags", "workflows",
    "groups", "users", "agents", "templates",
)
ACTIONS = ("view", "create", "edit", "delete")

# Grantable types are declared in `OBJECT_SPECS` (below); `OBJECT_TYPES` is derived from it.

# One entry per member of `FEATURES` (a missing one is a KeyError at import; `test_authz_policy.py`
# asserts the key sets are equal).
_FEATURE_LABELS = {
    "finance": "finance",
    "system": "system settings",
    "credentials": "credentials",
    "projects": "projects",
    "tasks": "tasks",
    "worktrees": "worktrees",
    "compliance": "compliance",
    # Labels name the subject as an operator reads the grid ("<Verb> <label>." is a sentence).
    "accounts": "credential accounts",
    "audit": "audit records",
    "crons": "scheduled activities",
    "nodes": "nodes",
    "sessions": "agentic sessions",
    "tags": "tag taxonomies",
    "workflows": "workflows",
    # "in your organization" distinguishes these from the fleet-wide `@superadmin_only` user/group
    # administration they can never reach.
    "groups": "groups in your organization",
    "users": "users in your organization",
    "agents": "SDK agents",
    "templates": "task templates",
}
_ACTION_VERBS = {
    "view": "View",
    "create": "Create",
    "edit": "Edit",
    "delete": "Delete",
}


def permission_key(feature: str, action: str) -> str:
    """The one rendering of a (feature, action) pair. Every producer of a key calls this."""
    return f"{feature}:{action}"


# The key that scopes `GET /api/projects`' answer, paired with the grant. Here rather than
# in a route module because `routes/portfolio.py` and `routes/users.py` both need it.
PROJECTS_VIEW_KEY = permission_key("projects", "view")
#: The keys the node, account and credential clauses read (`node_reach_clause`, `visible_account_clause`,
#: `visible_credential_clause`/`usable_credential_clause`).
NODES_VIEW_KEY = permission_key("nodes", "view")
ACCOUNTS_VIEW_KEY = permission_key("accounts", "view")
CREDENTIALS_VIEW_KEY = permission_key("credentials", "view")
CREDENTIALS_DELETE_KEY = permission_key("credentials", "delete")
COMPLIANCE_EDIT_KEY = permission_key("compliance", "edit")
#: An org's own task templates' write key, asked org-wide (`routes/task_templates.py`).
TEMPLATES_EDIT_KEY = permission_key("templates", "edit")


def permission_description(feature: str, action: str) -> str:
    """The catalog description for a key, derived rather than hand-typed."""
    return f"{_ACTION_VERBS[action]} {_FEATURE_LABELS[feature]}."


@dataclass(frozen=True)
class PermissionSpec:
    """One catalog entry, ordered by (feature, action) for the admin matrix and the seed."""

    key: str
    feature: str
    action: str
    description: str


PERMISSION_CATALOG: tuple[PermissionSpec, ...] = tuple(
    PermissionSpec(
        key=permission_key(feature, action),
        feature=feature,
        action=action,
        description=permission_description(feature, action),
    )
    for feature in FEATURES
    for action in ACTIONS
)

# Every key, as a set; `test_authz_policy.py` pins the count.
PERMISSION_KEYS = frozenset(spec.key for spec in PERMISSION_CATALOG)
#: Every key an Org Owner holds — the seat an acting operator is given (`owner_seat`).
OWNER_KEYS: frozenset[str] = expand_role(ROLES_BY_SLUG[access.OWNER_ROLE], PERMISSION_KEYS)


#: Keys the catalogue offers and nothing honours — a classification, not a boundary. Mirrored in
#: `apps/web/src/types.ts` (a parity test holds the two equal). No role decision reads it;
#: an API key's `grants_all` set and `GET /api/permissions`' `honoured` flag do (`app/api_keys.py`). Nothing
#: refuses or drops them: a role may carry one.
#: `finance:view` and `finance:edit` are honoured by routes (`routes/billing.py`, `routes/billing_plans.py`, both
#: also operator-plane only); when `finance:edit` gained its route only the built-in Org Owner/Admin roles
#: carried it, and those reach no operator-plane route, so the honouring granted nobody anything.
UNHONOURED_PERMISSION_KEYS: frozenset[str] = frozenset(
    {f"{feature}:{action}" for feature in ("crons", "workflows") for action in ACTIONS}
    | {
        # Portfolio Compliance gates on `view` and `edit` only (`routes/compliance_authoring.py`).
        "compliance:create", "compliance:delete",
        # The identity shell — gated by `@org_admin(Tenancy.CALLER_PLANE)` (an Org Owner/Admin
        # administers their own org), not these catalogue keys (`routes/users.py`, `routes/groups.py`).
        "users:create", "users:edit", "users:delete",
        "groups:create", "groups:edit", "groups:delete",
        # Read-and-narrow subjects with no mutating door.
        "audit:create", "audit:edit", "audit:delete",
        "sessions:create", "sessions:delete",
        # No route by design — `routes/nodes/__init__.py`'s docstring.
        "nodes:create",
        # No API route at all.
        "finance:create", "finance:delete",
        # SDK agents gate on `view` and `edit` only (`routes/agents.py`): registering, deleting and toggling a
        # catalog connector are edits, held by exactly the roles that carry `tasks:edit` (DD13).
        "agents:create", "agents:delete",
        # Task templates gate their writes on `edit` alone (`routes/task_templates.py`); every member reads them.
        "templates:view", "templates:create", "templates:delete",
    }
)


#: Keys a console surface honours with no route behind them. Declaring this makes the partition total:
#: every key has a route, is named here, or is withheld
#: (`test_every_routeless_key_is_withheld_or_declared_console_only`). Only keys a console surface
#: actually gates on belong here. Empty since `finance:view` gained a route (`routes/billing.py`).
CONSOLE_ONLY_PERMISSION_KEYS: frozenset[str] = frozenset()


# ── The author's own draft: the ONE conditional key in this module ──────────────────────
# `tasks:create` also admits the task-WRITE routes for a task its holder authored, while that task is
# still `draft`: filing a work item and shaping it before it runs are one act. Starting it is not, and
# stays `tasks:edit`. Three names because three readers: `declare` validates the last two at import,
# `_enforce` consults the first per request. Predicate: `authored_draft_task_clause`, below.

#: The key that admits under the fallback, checked against the catalog below (a stale spelling would
#: silently never fire).
AUTHOR_DRAFT_KEY = permission_key("tasks", "create")

#: The path param naming the draft; the guard reads the subject off the route's own path.
AUTHOR_DRAFT_PARAM = "task_id"

#: The actions the fallback may widen: content and delete. Not `view` (that is `tasks:view`).
AUTHOR_DRAFT_ACTIONS = frozenset({"edit", "delete"})

if AUTHOR_DRAFT_KEY not in PERMISSION_KEYS:
    raise RuntimeError(f"AUTHOR_DRAFT_KEY {AUTHOR_DRAFT_KEY!r} is not a member of PERMISSION_KEYS")
if AUTHOR_DRAFT_ACTIONS - set(ACTIONS):
    raise RuntimeError(
        f"AUTHOR_DRAFT_ACTIONS names actions outside ACTIONS: "
        f"{sorted(AUTHOR_DRAFT_ACTIONS - set(ACTIONS))}"
    )


# ── The user's own credential: the second conditional key in this module ───────
# Any authenticated session may create, read, edit and delete its own credentials without a
# `credentials:*` key, which still reaches someone else's row. Flag: `Declaration.own_credential`.
# Predicate: `owned_credential_clause`. Dispatcher: `scoped_credential_select_for`.

#: The one feature the flag is legal on, checked against `FEATURES` below.
CREDENTIALS_FEATURE = "credentials"

if CREDENTIALS_FEATURE not in FEATURES:
    raise RuntimeError(f"CREDENTIALS_FEATURE {CREDENTIALS_FEATURE!r} is not a member of FEATURES")


# ── The declaration a route carries ───────────────────────────────────────────────────────────────


class Access(str, Enum):
    """What KIND of authorization a route requires. Not a permission — a discriminator."""

    FEATURE = "feature"
    """`<feature>:<action>`, optionally object-scoped. The ordinary case."""

    FEATURE_ANY = "feature_any"
    """ANY ONE of an explicit set of feature keys. DISJUNCTION AT ONE ROUTE, NEVER IMPLICATION IN THE
    MODEL — and the difference is the whole reason this member is safe to have.

    An implication ("`worktrees:edit` implies `worktrees:view`") would change what a GRANT means
    everywhere, silently widening every route the implied key gates. This changes what ONE ROUTE
    accepts, and only for the keys it names in its own declaration. Nothing about any other route, any
    grant row, or the admin matrix moves; `Principal.permissions` stays the literal granted set.

    IT EXISTS FOR ONE SHAPE: a route serving a VERB SET that two different keys legitimately reach
    different parts of. Today that is `POST /api/commands`, the node command bus — `worktrees:edit`
    drives a worktree, `system:create` drives the fleet, the accounts and the node itself, and neither
    key implies the other. Before this member the only declaration both principals passed was
    `Access.ALWAYS`, which made the guard decide NOTHING and left a handler-side check as the sole
    thing between a zero-grant session and the bus.

    THE GUARD IS THE FLOOR, NOT THE WHOLE ANSWER, wherever this is used. A route declared here admits
    a caller holding ANY of the named keys; if some of its verbs need a narrower key than others, the
    handler still has to say so (`app/routes/commands.py::COMMAND_TYPE_KEYS` is that refinement). What
    the floor buys is that everyone NOT holding one of these keys — `worktrees:view`, a zero-grant
    session, a revoked one — is denied here, in the kernel, which is where this module's whole design
    says denial belongs.

    DO NOT REACH FOR IT TO AVOID A DECISION. Two keys because the route genuinely serves two
    capabilities is the case. Two keys because it was unclear which one applied is a route that needs
    splitting, and `declare` cannot tell the difference — review has to."""

    SUPERADMIN = "superadmin"
    """`is_superadmin` ONLY. NO FEATURE KEY CAN EVER REACH IT, and that is the escalation path the
    settled design deliberately closed: user, group and permission administration stay here, so no
    grantable capability can ever be used to grant more capability."""

    ORG_ADMIN = "org_admin"
    """The TENANT-ADMIN gate (G4): an operator (`Principal.is_operator`), or an Org Owner/Admin
    (`Principal.org_role`) of the caller's own org. NO FEATURE KEY REACHES IT, like `SUPERADMIN`: user,
    group and org administration stay behind an org ROLE, so no project-scoped key can be used to grant
    more capability. The caller's own org is what the seam binds; `Tenancy.OPERATOR` is refused for it
    at import (a tenant admin never reads fleet-wide)."""

    INFRA = "infra"
    """The agent/node principal surface (`/api/agent/*`, `/api/node-bus`, `/api/console-stream`, and
    `/api/node-bootstrap`) — always permitted, protected by `NODE_TOKEN` alone exactly as it is today.
    An operator-chosen accepted residual: it is to be recorded as a residual and never presented as
    enforcement.

    THE FIRST THREE ARE REFUSED BY THE PUBLIC ALB; `/api/node-bootstrap` IS DELIBERATELY NOT, and that
    asymmetry is the one thing a reader of this docstring must not smooth over — this enum's members do
    not share a reachability story, so "INFRA is tailnet-only" is no longer a safe reading. Why that one
    is exempt and what bounds it: `app/node_bootstrap.py::BOOTSTRAP_PATH`."""

    ALWAYS = "always"
    """Any authenticated session (or a public route) — no grant needed. For routes whose response
    contains nothing about anyone: the catalog, health, the login surface."""


class Tenancy(str, Enum):
    """Which tenancy plane a route runs on — the second axis of its declaration, orthogonal to `Access`.

    `Access` answers who may call; this answers what the call may see (a tenancy hole serves the right
    caller another org's rows). No default, deliberately: a route that omits it fails at import. The axes
    are independent (e.g. `POST /api/auth/login` is `BOOTSTRAP`, `GET /api/stream` is `ORG_SCOPED`, both
    `ALWAYS`); the route census in the API test suite checks both."""

    ORG_SCOPED = "org_scoped"
    """The read scope is ONE org: the principal's own. An operator's READ (GET/HEAD) binds the operator
    scope instead — the same route, a wider plane, decided by who is calling rather than by the
    declaration (`_principal_scope`); their mutations stay on their own org.

    THE VERDICT THE SESSION PLANE CARRIES BY CONSTRUCTION, not by convention: a `FEATURE`/`FEATURE_ANY`
    route is reached only by resolving a principal, and a resolved principal always carries an org.
    `feature` and `feature_any` therefore set this term themselves rather than making ~170 call sites
    repeat it, and `declare` refuses any other term on those access classes. The census asserts that
    coverage rather than assuming it."""

    OPERATOR = "operator"
    """The operator plane: fleet-wide BY DESIGN, and the design is what makes this safe to write down.

    It is not "unscoped because nobody scoped it" — that state is `ORG_SCOPED` with nothing bound,
    which `criteria_for` renders as `false()` and which reads NOTHING. This term is a positive claim
    that the route's SUBJECT is the fleet: the node pool, the accounts, the control plane's own
    settings and sweeps.

    THE ADMISSION RULE IS ABOUT THE SUBJECT, NOT ABOUT THE TABLES, and the distinction has to be
    spelled out because the obvious reading is wrong. "Its rows have no org to belong to" would admit
    almost none of the routes wearing this term: `Node`, `Account`, `Cron`, `Alert` and `PortGridVar`
    are all `Stored` in `app/tenancy/manifest.py` — every one of them carries its own
    `org_id`. What makes a route operator-plane is that the QUESTION IT ANSWERS is a fleet question —
    "which nodes are alive", "what is the release every node should run", "which epics still need a
    fan-out" — so an answer narrowed to one tenant would be a wrong answer rather than a safe one. A
    route whose subject is one tenant's rows is `ORG_SCOPED` even when its caller is fleet
    infrastructure, because the ARM the caller arrives on is what widens it
    (`routes/agent/auth.py::_read_scope_for`), never the declaration.

    IT IS AN ASSERTION NOTHING DOWNSTREAM WILL SECOND-GUESS, so it is written down per route rather
    than inferred: `test_authz_policy.py` holds one allowlist-with-reasons per access class that can
    wear it (`_OPERATOR_PLANE_ALLOWLIST` for `SUPERADMIN`, `_INFRA_OPERATOR_PLANE_ALLOWLIST` for
    `INFRA`), each asserted as an EQUALITY so a route cannot join or leave the plane silently."""

    CALLER_PLANE = "caller_plane"
    """ONE ROUTE, TWO PLANES, DECIDED BY WHO IS CALLING: fleet-wide for an operator principal
    (`Principal.is_operator` — superadmin AND a member of the operator plane), the principal's own org for
    everyone else — a tenant superadmin, a feature-key holder.

    WHY IT EXISTS: there is ONE console. A tenant admin works the same page, over the same
    route, as the operator — the Directory, the node roster, node groups, crons — and sees their own org's
    rows where the operator sees every org's. Before this term that took two routes per subject (an
    `OPERATOR` one and an `ORG_SCOPED` twin), which is the duplication the single console retires.

    SAFE FOR A GRANTABLE KEY, which is why `declare` admits it on `FEATURE`/`FEATURE_ANY` where it refuses
    `OPERATOR`: the widening is keyed on `is_operator`, which no key can grant, and an operator already
    holds allow-all. A key holder is bound to their own org exactly as under `ORG_SCOPED`."""

    BOOTSTRAP = "bootstrap"
    """Genuinely runs BEFORE anything can be bound — so nothing is bound, and the handler does its
    identity reads inside `app/tenancy/seam.py::identity_scope()`.

    IT IS A PROPERTY OF THE GUARD'S BEHAVIOUR ON THIS ROUTE, not of whether a cookie was presented.
    `GET /api/auth/me` is reached with a verified session and is still `BOOTSTRAP`: `authz_guard`
    answers an `ALWAYS` declaration from the declaration ALONE and never resolves a principal, so there
    is no org for it to bind and the handler's reads run with nothing in force. "Before anything can be
    bound" means exactly that, and it is checkable — a `BOOTSTRAP` route is one no bind site lies on.

    THE ESCAPE IS THE POINT, not an afterthought. Most of the `ALWAYS` routes read tenant-owned tables
    with no scope in force, and `routes/auth.py`'s login selects `AdminUser` by LOWER-CASED EMAIL
    rather than by primary key — so under an enforcing seam, with no scope bound and no escape, nobody
    could log in at all. `identity_scope()` is what lets identity resolve before there is an identity
    to scope by; a route declared here without it is a route that will fail closed at the flip.

    KNOWN GAP, NAMED RATHER THAN CLAIMED CLOSED: the `ALWAYS` routes that read tenant-owned tables do
    NOT yet run those reads inside `identity_scope()` — `login`, `google_login`, `logout`, and both
    halves of `/api/auth/me`, whose `_admin_out` fan-out calls `effective_permissions` directly. The
    verdict is right; the escape those handlers need is the seam's own release, and this is where the
    flip should look for the list.

    NEVER A CONVENIENCE. "This route is awkward to scope" is `ORG_SCOPED` and a bug to fix, not this."""
