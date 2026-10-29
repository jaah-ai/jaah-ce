"""The built-in role catalog — the single home for what each role is and which permission keys it expands to.

Platform data, not tenant data: revision 0286 INSERTs `roles`/`role_permissions` from `ROLES`, and
`app/seed.py::_seed_roles` converges them from the same tuple, so the table and this file cannot drift.
Tenants cannot create, edit or delete a role (no write route exists; `app/routes/access.py`).

Keys stay the enforcement vocabulary (`app/authz/catalog.py::PERMISSION_KEYS`); a role is only a named bundle of
them. STDLIB ONLY, like `app/tenancy/ids.py`: an alembic revision imports this leaf and must not import
the ORM. So the org roles' "every key" is the `ALL_KEYS` sentinel rather than a copy of the vocabulary, and
`expand()` resolves it against whatever vocabulary its caller holds (`authz.PERMISSION_KEYS` in code, the
`permissions` table in a revision). The invariants below are pinned by `tests/test_access_catalog.py`.
"""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

#: The two levels a role lives at (`roles.level`, CHECKed).
LEVEL_ORG = "org"
LEVEL_PROJECT = "project"
LEVELS: tuple[str, ...] = (LEVEL_ORG, LEVEL_PROJECT)

#: The scopes a binding can name (`role_bindings`' exactly-one-scope columns). No node or credential scope.
SCOPE_ORG = "org"
SCOPE_PROJECT = "project"
SCOPE_ACCOUNT = "account"
SCOPES: tuple[str, ...] = (SCOPE_ORG, SCOPE_PROJECT, SCOPE_ACCOUNT)

#: Features whose keys a project-level role grants on the resource it is bound to.
PROJECT_LEVEL_FEATURES: frozenset[str] = frozenset(
    {"projects", "tasks", "worktrees", "sessions", "crons", "workflows", "agents"}
)
#: Features that belong to the org as a whole. With the set above, a partition of `authz.FEATURES`.
ORG_LEVEL_FEATURES: frozenset[str] = frozenset(
    {"finance", "system", "credentials", "compliance", "accounts", "audit", "nodes", "tags", "groups",
     "users", "templates"}
)

#: The only org-level keys a project role may carry — what a developer needs to bring a credential or an
#: account and to read tags. Every other org-level key is an Org Owner/Admin's.
PROJECT_ROLE_ORG_KEYS: frozenset[str] = frozenset(
    {"credentials:view", "credentials:create", "accounts:view", "accounts:create", "tags:view"}
)

#: Keys that widen what a principal can reach beyond the resource they hold them on (placement, fleet
#: config, credential material). Disjoint from `PROJECT_ROLE_ORG_KEYS` by invariant.
WIDENING_KEYS: frozenset[str] = frozenset(
    {"worktrees:create", "worktrees:delete", "credentials:edit", "nodes:edit", "system:view",
     "system:create",
     # A project role registers agents and edits its own; another's agent, or a connector switched on for
     # the whole org, runs in every project, so that takes the key org-wide (`routes/agents.py`).
     "agents:edit"}
)


#: An access request's lifecycle (`access_requests.state`).
REQUEST_STATES: tuple[str, ...] = ("pending", "approved", "denied", "withdrawn")


def in_sql(column: str, values: tuple[str, ...]) -> str:
    """`column IN ('a', 'b')` — a CHECK over one of the closed vocabularies above, rendered from it so the
    model and the revision cannot drift from the constant (values are fixed literals, never input)."""
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


# The binding-shaped tables' CHECK bodies, read by `app/models/` and held equal to revision 0300's own
# literal (a revision cannot import app code): exactly one scope, and an org scope naming the row's own org.
ONE_SCOPE_SQL = (
    "(scope_org_id IS NOT NULL)::int + (project_id IS NOT NULL)::int + (account_id IS NOT NULL)::int = 1"
)
SCOPE_ORG_IS_OWN_SQL = "scope_org_id IS NULL OR scope_org_id = org_id"


class _AllKeys:
    """`ALL_KEYS`: the whole vocabulary, whatever it holds when `expand()` is called."""

    def __repr__(self) -> str:
        return "ALL_KEYS"


ALL_KEYS = _AllKeys()


class Role(NamedTuple):
    """One built-in role. `id` is fixed across environments so a revision and a seed name the same row."""

    id: UUID
    slug: str
    name: str
    description: str
    level: str
    rank: int
    keys: frozenset[str] | _AllKeys
    valid_scopes: tuple[str, ...]


def _all(feature: str) -> frozenset[str]:
    return frozenset(f"{feature}:{action}" for action in ("view", "create", "edit", "delete"))


_PROJECT_VIEWER_KEYS = frozenset(
    {"projects:view", "tasks:view", "worktrees:view", "sessions:view", "tags:view"}
)
_PROJECT_DEVELOPER_KEYS = _PROJECT_VIEWER_KEYS | {
    "tasks:create", "tasks:edit", "tasks:delete", "worktrees:edit",
    # Whoever files and edits tasks registers and picks the SDK agents that run them (DD13).
    "agents:view", "agents:edit",
    "credentials:view", "credentials:create", "accounts:view", "accounts:create",
}
_PROJECT_MAINTAINER_KEYS = (
    _PROJECT_DEVELOPER_KEYS
    | {"projects:edit", "worktrees:create", "worktrees:delete", "sessions:edit"}
    | _all("crons")
    | _all("workflows")
)

_ORG_ONLY: tuple[str, ...] = (SCOPE_ORG,)

# UUIDv7 by construction, in the hand-assigned block `SYSTEM_ACTOR_ID` and `app/tenancy/ids.py` use.
ROLES: tuple[Role, ...] = (
    Role(
        id=UUID("01900000-0000-7000-8000-000000000020"),
        slug="org_owner",
        name="Org Owner",
        description="Full control of the organization, including who else is an owner.",
        level=LEVEL_ORG,
        rank=100,
        keys=ALL_KEYS,
        valid_scopes=_ORG_ONLY,
    ),
    Role(
        id=UUID("01900000-0000-7000-8000-000000000021"),
        slug="org_admin",
        name="Org Admin",
        description="Administers the organization: its projects, people, credentials and settings.",
        level=LEVEL_ORG,
        rank=90,
        keys=ALL_KEYS,
        valid_scopes=_ORG_ONLY,
    ),
    Role(
        id=UUID("01900000-0000-7000-8000-000000000022"),
        slug="project_maintainer",
        name="Project Maintainer",
        description="Runs a project: its settings, worktrees, sessions, schedules and workflows.",
        level=LEVEL_PROJECT,
        rank=30,
        keys=_PROJECT_MAINTAINER_KEYS,
        valid_scopes=SCOPES,
    ),
    Role(
        id=UUID("01900000-0000-7000-8000-000000000023"),
        slug="project_developer",
        name="Project Developer",
        description="Works on a project: creates and edits tasks and brings their own credentials.",
        level=LEVEL_PROJECT,
        rank=20,
        keys=_PROJECT_DEVELOPER_KEYS,
        valid_scopes=SCOPES,
    ),
    Role(
        id=UUID("01900000-0000-7000-8000-000000000024"),
        slug="project_viewer",
        name="Project Viewer",
        description="Reads a project: its tasks, worktrees and sessions.",
        level=LEVEL_PROJECT,
        rank=10,
        keys=_PROJECT_VIEWER_KEYS,
        valid_scopes=SCOPES,
    ),
)

ROLES_BY_SLUG: dict[str, Role] = {role.slug: role for role in ROLES}


def expand(role: Role, vocabulary: frozenset[str]) -> frozenset[str]:
    """The keys `role` grants, drawn from `vocabulary` — the whole of it for an `ALL_KEYS` role.

    Refuses a named key the vocabulary lacks: a role that silently lost a key would grant less than it
    says, and a seed or revision writing it would record the loss as data."""
    if isinstance(role.keys, _AllKeys):
        return frozenset(vocabulary)
    unknown = sorted(role.keys - vocabulary)
    if unknown:
        raise ValueError(
            f"role {role.slug!r} names permission key(s) {unknown} the vocabulary does not carry "
            "(app/access_catalog.py::ROLES; the vocabulary is app/authz/catalog.py::PERMISSION_KEYS)"
        )
    return role.keys
