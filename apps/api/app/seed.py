"""Idempotent seed — bootstrap the admin user, the e2e fixture logins, the role catalog and their bindings.

Access is role bindings only (`app/access.py`): the `permissions` rows are the key vocabulary the built-in
roles expand to (`_seed_permissions` + `_seed_roles`), and every seeded identity's reach is a
`role_bindings` row. No key or grant row is written anywhere.

No password default lives here (this runs in production). Fail closed:
  * ADMIN_PASSWORD unset/empty raises RuntimeError, stopping `migrate && seed && exec uvicorn` rather
    than creating a guessable account.
  * An unset e2e fixture password skips that row with a printed reason.

The three fixture logins and a fixture second tenant (`_seed_fixture_org`, never the operator org) also
require an affirmative APP_ENV (`_fixture_app_env`): isolation assertions need a second tenant before
any browser opens, and synthetic rows belong only in throwaway environments (migration 0118 neutralised
old ones). *_EMAIL vars default; a system_state marker gates the bootstrap once, so a deleted admin
is never resurrected.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import access, edition, edition_features, identities, plan_terms
from app.dev_bypass import FIXTURE_APP_ENVS
from app.auth import hash_password
from app.access_catalog import ROLES, ROLES_BY_SLUG, expand
from app.authz.catalog import PERMISSION_CATALOG, PERMISSION_KEYS
from app.db import SessionLocal
from app.models.access import Group, Permission, Role, RoleBinding, RolePermission
from app.models.control_plane import SystemState
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.tenancy.ids import OPERATOR_ORG, ORG_ZERO_ID

# These constants exist because tests (tests/conftest.py, tests/test_authz_enforcement.py) and
# `app/seed_dev_profile.py` import them. `seed()` re-reads the vars at call time (see `_password`).
# No `E2E_ORGADMIN_B_PASSWORD` constant: nothing imports it.
_DEFAULT_ADMIN_EMAIL = "admin@example.com"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", _DEFAULT_ADMIN_EMAIL)
# No default: "" means no credential configured, which refuses below.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# Dedicated Playwright login (the web e2e suite), separate from ADMIN_EMAIL so synthetic traffic is
# attributable. Seeded only in a `_FIXTURE_APP_ENVS` environment with E2E_ADMIN_PASSWORD set.
_DEFAULT_E2E_ADMIN_EMAIL = "e2e-smoke@cockpit.local"
E2E_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", _DEFAULT_E2E_ADMIN_EMAIL)
E2E_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "")
# Dedicated non-superadmin login for the RequireSuperadmin-gated routes. Same rationale and gates.
_DEFAULT_E2E_NONSUPERADMIN_EMAIL = "e2e-nonsuperadmin@cockpit.local"
E2E_NONSUPERADMIN_EMAIL = os.environ.get("E2E_NONSUPERADMIN_EMAIL", _DEFAULT_E2E_NONSUPERADMIN_EMAIL)
E2E_NONSUPERADMIN_PASSWORD = os.environ.get("E2E_NONSUPERADMIN_PASSWORD", "")
# The org-admin of fixture org B, the principal isolation assertions sign in as. `is_superadmin=False`
# is the point: a superadmin's allow-all bypasses the catalog and every read-seam predicate. Same gates.
_DEFAULT_E2E_ORGADMIN_B_EMAIL = "e2e-orgadmin-b@cockpit.local"
E2E_ORGADMIN_B_EMAIL = os.environ.get("E2E_ORGADMIN_B_EMAIL", _DEFAULT_E2E_ORGADMIN_B_EMAIL)
SEED_MARKER_KEY = "seed_bootstrap"

# Fixture org B. Never the operator org (`app/tenancy/ids.py`): real orgs come from the console.
# The id is fixed to match the org B created on the dev control plane, so every environment names the
# same tenant. UUIDv7, like its siblings.
FIXTURE_ORG_B_ID = uuid.UUID("01a05f8a-ec2e-7e01-8144-909a57017fd1")
FIXTURE_ORG_B_SLUG = "org-b"
FIXTURE_ORG_B_NAME = "Org B"

# The org-admin fixture's reach: `org_admin` at org B (`_bind_org_role`). Its old direct key set named
# org-level and widening keys (`nodes:edit`, `system:view`, `users:view`…), which only an org role carries.
ORGADMIN_B_ROLE = "org_admin"
# Every seeded superadmin also OWNS its own org, so its reach survives on the binding path. The flag is
# kept: each holds a seat in org zero, the operator plane (`app/tenancy/ids.py::is_operator_plane`), and only
# that seat is operator — a tenant seat the same person may also hold is an ordinary tenant's.
SUPERADMIN_ROLE = "org_owner"

# The throwaway-environment allowlist lives in `app/dev_bypass.py::FIXTURE_APP_ENVS` (shared with the
# credential store's published-keypair rule); only such an environment gets a fixture login.
_FIXTURE_APP_ENVS = FIXTURE_APP_ENVS

def _fixture_app_env() -> str | None:
    """The declared APP_ENV if it is one that may hold e2e fixture logins, else None.

    An absent APP_ENV denies: a `get("APP_ENV", "local")` default would hand an unconfigured real
    environment a synthetic superadmin: absence is not a state.
    """
    value = os.environ.get("APP_ENV", "").strip().lower()
    # Never in the community edition, whatever APP_ENV says: its one user is its only login (AC-12).
    return value if value in _FIXTURE_APP_ENVS and not edition.IS_CE else None


def _password(var: str) -> str:
    """A password read from the environment at call time, never captured at import.

    As in the rest of the env-driven auth code, so a test's monkeypatch is honoured. Returned verbatim
    (whitespace may be part of a secret) and never printed or logged. Callers test emptiness with `.strip()`.
    """
    return os.environ.get(var, "")


def _seed_fixture_login(
    session, *, email: str, password_var: str, superadmin: bool, org_id: uuid.UUID = ORG_ZERO_ID
) -> bool:
    """Create one e2e fixture login if it is missing and `password_var` carries a value.

    Skips (returns False, printed with the variable to set) where the primary admin refuses: fixtures are
    optional. `org_id` defaults to org zero, the fixture org the dev profile seeds its rows into
    (`app/seed_dev_profile.py::DEV_ORG_ID`); the org-admin fixture passes org B and gets its binding in a
    later step (it needs the roles). Binds nothing and never changes an existing row's org. A new
    superadmin login joins its org's Everyone group (`access.join_everyone`).
    """
    existing = identities.first_membership(session, email)
    if existing is not None:
        _adopt_org(existing, org_id)
        return False
    password = _password(password_var)
    if not password.strip():
        print(f"seed: {password_var} is unset — no {email} fixture login (optional; set it to get one)")
        return False
    row = AdminUser(
        email=email,
        password_hash=hash_password(password),
        role="admin",
        is_superadmin=superadmin,
        activated_at=datetime.now(timezone.utc),
        # Org written explicitly, as for the bootstrap admin below.
        org_id=org_id,
    )
    session.add(row)
    if superadmin:
        session.flush()
        access.join_everyone(session, row)
    return True


def _adopt_org(user: AdminUser, org_id: uuid.UUID = ORG_ZERO_ID) -> None:
    """Give a seeded admin an org if it has none. Idempotent, and a repair rather than a policy.

    Never moves a user that already has one (else `--ensure` would drag the org-B fixture back to org
    zero). Needed because an existing row takes the early return and pre-stamp databases hold NULL;
    an org-less admin stamps rows with no org, so nothing it owns can be placed
    (`routes/console.py::_claim_slot_for_console_user`). Only for rows seed owns; general backfills are
    migrations' job.
    """
    if user.org_id is None:
        user.org_id = org_id


def _seed_fixture_org(session) -> bool:
    """Ensure the fixture second tenant (org B) exists. Returns whether this call inserted it.

    Called only inside the `_fixture_app_env()` gate. Executed immediately, not `session.add`-ed: the
    org-admin login's FK and `_seed_app_settings`' `SELECT id FROM orgs` need the row present, not reliant
    on flush ordering. `_seed_app_settings` gives it its branding row. Bare `ON CONFLICT DO NOTHING` for
    `_seed_orgs`' reason (two unique surfaces); `RETURNING` reports the real insert. An edition with one org has no
    second tenant.
    """
    if not edition_features.enabled("multi_org_tenancy"):
        return False
    from app.signup_plans import PRESELECTED

    inserted = session.scalars(
        pg_insert(Org)
        .values([{
            "id": FIXTURE_ORG_B_ID,
            "slug": FIXTURE_ORG_B_SLUG,
            "name": FIXTURE_ORG_B_NAME,
            # The plan a self-serve org starts on, as before the Default plan went.
            "plan_key": PRESELECTED,
            "description": (
                "The e2e fixture second tenant. Seeded ONLY where APP_ENV is local/ci/test "
                "(app/seed.py::_seed_fixture_org) so an isolation assertion has another tenant to be "
                "isolated from before any browser opens. A real environment "
                "gets its further orgs from the console, never from the seed."
            ),
        }])
        .on_conflict_do_nothing()
        .returning(Org.id)
    ).all()
    return len(inserted) == 1


def _seed_fixture_everyone(session, org_id: uuid.UUID, *member_emails: str) -> bool:
    """Ensure a fixture org's Everyone group (`access.everyone_group`) and that each named login in that org is a
    member (`access.join_everyone`). Idempotent; returns whether the group was missing."""
    session.flush()
    missing = session.scalar(
        select(Group.id).where(Group.org_id == org_id, Group.is_everyone.is_(True)).limit(1)
    ) is None
    access.everyone_group(session, org_id)
    for email in member_emails:
        user = identities.first_membership(session, email)
        if user is not None and user.org_id == org_id:
            access.join_everyone(session, user)
    return missing


def _bind_org_role(session, *, email: str, role: str, org_id: uuid.UUID | None = None) -> int:
    """Bind `role` (an `access_catalog.ROLES` slug) to the named user at its own org. Returns how many this
    call inserted (0 or 1). `org_id` picks the person's seat in that org; omitted, their first membership.

    Written here, not left to a migration: the seed runs after migrations, so a seeded identity's reach
    must be a binding already. `org_id` written explicitly (nothing bound at boot). Idempotent on
    `uq_role_bindings`; deletes nothing."""
    user = identities.first_membership(session, email)
    if user is not None and org_id is not None:
        user = identities.membership_in(session, user.identity_id, org_id)
        if user is None or not user.is_superadmin:
            return 0  # a named org's seat is bound only when it is a superadmin seat
    if user is None or user.org_id is None:
        # Expected when the login was skipped for want of a password; printed so a later failure has a cause.
        print(f"seed: no {email} row — no {role} binding (the login itself was skipped)")
        return 0
    return len(session.scalars(
        pg_insert(RoleBinding)
        .values(org_id=user.org_id, user_id=user.id, role_id=ROLES_BY_SLUG[role].id,
                scope_org_id=user.org_id)
        .on_conflict_do_nothing(constraint="uq_role_bindings")
        .returning(RoleBinding.id)
    ).all())


def bind_superadmin(session, email: str) -> int:
    """A seeded superadmin's `SUPERADMIN_ROLE` bindings: at its first seat's org and, explicitly, org zero (the
    same row on a fresh database). Returns how many this call inserted. After `_seed_roles`."""
    return sum(
        _bind_org_role(session, email=email, role=SUPERADMIN_ROLE, org_id=org_id) for org_id in (None, ORG_ZERO_ID)
    )


#: Fixture GROUPS bound org-wide to a role, for an e2e spec's own identity to join over
#: `PUT /api/groups/{id}/members`: with the grant PUTs retired and no binding write route yet, membership is
#: how a spec gives a user of its own a role. Developers: the get-started card's e2e spec; the
#: company owners (in their own tenant, `E2E_COMPANY_ORG_SLUG`): the company-setup card's e2e spec.
E2E_DEVELOPERS_GROUP = "E2E Project Developers"
E2E_COMPANY_OWNERS_GROUP = "E2E Company Owners"
E2E_COMPANY_ORG_SLUG = "e2e-company-setup"
#: A PENDING self-service org and its signed-up owner (an `org_signups` row), for the nine-step card and the
#: Billing page: the self-service company-setup e2e spec. Seeded by
#: `app/seed_dev_profile.py::_e2e_fixture_groups`; the spec sets the owner's password.
E2E_SELF_SERVICE_ORG_SLUG = "e2e-self-service"
E2E_SELF_SERVICE_OWNER_EMAIL = "e2e-self-owner@cockpit.local"


def _seed_fixture_group(session, *, name: str, role: str, org_id: uuid.UUID = ORG_ZERO_ID) -> int:
    """The named group in `org_id`, bound org-wide to `role`. Returns how many bindings this call inserted
    (0 or 1). Idempotent: an existing group is reused, the binding upserted."""
    group = session.scalar(select(Group).where(Group.name == name, Group.org_id == org_id))
    if group is None:
        group = Group(name=name, description="E2E fixture group.", org_id=org_id)
        session.add(group)
        session.flush()
    return len(session.scalars(
        pg_insert(RoleBinding)
        .values(org_id=group.org_id, group_id=group.id, role_id=ROLES_BY_SLUG[role].id,
                scope_org_id=group.org_id)
        .on_conflict_do_nothing(constraint="uq_role_bindings")
        .returning(RoleBinding.id)
    ).all())


def _seed_permissions(session) -> int:
    """Ensure every key in the authz catalog has a `permissions` row. Returns how many were inserted.

    Seeds from `app.authz.catalog.PERMISSION_CATALOG`, never a typed list, so table and catalog cannot disagree.
    Binds nothing and deletes nothing (retiring a key is a migration's act). `ON CONFLICT (key) DO
    NOTHING` so concurrent replicas cannot race; `RETURNING` reports real inserts.
    """
    inserted = session.scalars(
        pg_insert(Permission)
        .values([
            {"key": spec.key, "description": spec.description}
            for spec in PERMISSION_CATALOG
        ])
        .on_conflict_do_nothing(index_elements=["key"])
        .returning(Permission.key)
    ).all()
    return len(inserted)


def _seed_roles(session) -> int:
    """Converge `roles`/`role_permissions` onto `app/access_catalog.py::ROLES`. Returns rows changed.

    Revision 0286 inserted them; this re-creates a deleted row, rewrites a drifted label and makes each
    built-in's keys EXACTLY its expansion over `authz.PERMISSION_KEYS` — a key added to the vocabulary
    reaches the `ALL_KEYS` roles here. Built-ins only: nothing else exists (no custom roles). Runs after
    `_seed_permissions`, whose rows the expansion names."""
    changed = 0
    for role in ROLES:
        changed += len(session.scalars(
            pg_insert(Role)
            .values(id=role.id, slug=role.slug, name=role.name, description=role.description,
                    level=role.level, rank=role.rank, builtin=True)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"slug": role.slug, "name": role.name, "description": role.description,
                      "level": role.level, "rank": role.rank, "builtin": True},
                where=(
                    (Role.slug != role.slug) | (Role.name != role.name)
                    | (Role.description != role.description) | (Role.level != role.level)
                    | (Role.rank != role.rank) | ~Role.builtin
                ),
            )
            .returning(Role.id)
        ).all())
    ids = dict(session.execute(select(Permission.key, Permission.id)).all())
    wanted = {
        # A key `_seed_permissions` did not leave in the table is a KeyError, never a silently short role.
        (role.id, ids[key]) for role in ROLES for key in expand(role, PERMISSION_KEYS)
    }
    held = set(session.execute(
        select(RolePermission.role_id, RolePermission.permission_id)
        .where(RolePermission.role_id.in_([role.id for role in ROLES]))
    ).all())
    for role_id, permission_id in held - wanted:
        session.execute(
            RolePermission.__table__.delete().where(
                (RolePermission.role_id == role_id) & (RolePermission.permission_id == permission_id)
            )
        )
    missing = sorted(wanted - held, key=str)
    if missing:
        session.execute(
            pg_insert(RolePermission)
            .values([{"role_id": role_id, "permission_id": pid} for role_id, pid in missing])
            .on_conflict_do_nothing()
        )
    return changed + len(held - wanted) + len(missing)


def _seed_orgs(session) -> int:
    """Ensure the operator org's row exists. Returns how many this call actually inserted.

    Seeds `app.tenancy.ids.OPERATOR_ORG` only: every other org is a tenant whose deletion
    stays deleted. On every supported path the baseline revision already inserted it, so this is convergence
    and an idempotence oracle.

    Bare `ON CONFLICT DO NOTHING`, not `(id)`: `uq_orgs_slug` is a second unique surface, and a slug
    collision must not abort the boot. Cost: a squatted slug also reports 0, silently; detecting it is
    deferred.
    """
    inserted = session.scalars(
        pg_insert(Org)
        .values([
            {
                "id": org.id,
                "slug": org.slug,
                "name": org.name,
                "description": org.description,
                "plan_key": plan_terms.DEFAULTS_KEY,
            }
            for org in (OPERATOR_ORG,)
        ])
        .on_conflict_do_nothing()
        .returning(Org.id)
    ).all()
    return len(inserted)


def _seed_app_settings(session) -> int:
    """Give every org a branding row it does not already have. Returns how many this call inserted.

    Keeps `count(app_settings) == count(orgs)` converging; reads the table, not `OPERATOR_ORG`, so console-
    created orgs converge too. Sets no title (the server default owns it). The write path is
    `routes/settings.py::ensure_app_settings`; this is the backstop.
    """
    # Deferred import: `app.routes.settings` pulls the FastAPI route graph into this CLI and would cycle.
    from app.routes.settings import ensure_app_settings
    from app.tenancy import seam

    # Fleet-wide plane: the seed runs unbound, and an unbound read under the enforcing seam returns nothing
    # (reading as a healthy 0). Flushed first so rows built earlier are not autoflushed on a different
    # plane (write-neutral, but what the scope census flags). `seed()` already wraps its body in this
    # escape, so the `with` below is a re-entry no-op.
    session.flush()
    with seam.operator_scope():
        created = 0
        for org_id in session.scalars(select(Org.id).order_by(Org.slug)).all():
            if ensure_app_settings(session, org_id):
                created += 1
        return created


def create_admin(session, email: str, password: str, **fields) -> AdminUser:
    """The bootstrap admin: a superadmin seat in org zero, in its Everyone group. Flushes, never commits.

    Two callers: `_seed` (ADMIN_EMAIL/ADMIN_PASSWORD) and the community edition's first run
    (`app/routes/ce_setup.py`), which passes the fields its single user needs on top (`fields`)."""
    admin = AdminUser(
        email=email,
        password_hash=hash_password(password),
        role="admin",
        is_superadmin=True,
        activated_at=datetime.now(timezone.utc),
        # Org zero written explicitly, and first, so `identities.first_membership` is the operator seat:
        # `AdminUser` is `Stored` and the seam stamps from the bound scope, which is empty at boot. An
        # org-less admin cannot claim a console slot (`routes/console.py::_claim_slot_for_console_user`), so
        # the drain would lease and reset the worktree the operator is working in.
        org_id=ORG_ZERO_ID,
        **fields,
    )
    session.add(admin)
    session.flush()
    access.join_everyone(session, admin)
    return admin


def seed(session) -> dict:
    """Every row this control plane needs to exist before anyone can log in. Fleet-wide.

    Runs on the operator plane: it creates the orgs, so there is nothing to bind to, and under the
    enforcing seam an unbound read returns nothing — every idempotence check would miss and the INSERT
    would hit the unique index, so the CP never starts. Write-neutral (`write_seam.write_scope_kind_of`);
    each row names its own org. Scoped over the whole function so no row autoflushes on another plane.
    """
    from app.tenancy import seam

    with seam.operator_scope():
        return _seed(session)


def _seed(session) -> dict:
    # No row carries a label: a person's derives from their name pair, else the email
    # (`identities.person_label`). No `is_system` row may be created here (migration 0105 owns
    # the only one). Every key is present on every path so the summary accounts for every row.
    created = {
        "admin": False,
        "e2e_admin": False,
        "e2e_nonsuperadmin": False,
        # The org-admin fixture's three row-classes (tenant, login, binding), each with its own gate, reported
        # separately so a CI log can tell "login holds nothing" from "fixture skipped".
        "fixture_org": False,
        "e2e_orgadmin_b": False,
        "e2e_orgadmin_b_binding": 0,
        # Org-zero seats this call added to a login that already existed elsewhere (an existing database).
    }

    # Re-read at call time (see `_password`); the constants are the import-time fallback.
    admin_email = os.environ.get("ADMIN_EMAIL", _DEFAULT_ADMIN_EMAIL)
    admin = identities.first_membership(session, admin_email)
    # The community edition's one user is made by its first run (`app/routes/ce_setup.py`), never from the
    # environment: no ADMIN_PASSWORD exists on a person's own machine.
    if admin is None and not edition.IS_CE:
        admin_password = _password("ADMIN_PASSWORD")
        if not admin_password.strip():
            # Names the variable, never the value. The bootstrap admin is unconditional (terraform injects
            # ADMIN_PASSWORD from Secrets Manager), so raising makes a missing credential stop the boot visibly.
            raise RuntimeError(
                "ADMIN_PASSWORD is unset or empty — refusing to bootstrap the admin account "
                f"{admin_email!r} without a credential (this module carries no default password)."
            )
        admin = create_admin(session, admin_email, admin_password)
        created["admin"] = True
    elif admin is not None:
        _adopt_org(admin)
        if admin.role != "admin":
            admin.role = "admin"

    # The e2e fixture logins, behind the APP_ENV gate. A deployed CP (`APP_ENV=production`, or unset) skips.
    fixture_env = _fixture_app_env()
    if fixture_env is None:
        print(
            f"seed: APP_ENV is {os.environ.get('APP_ENV') or '<unset>'!r}, not one of "
            f"{sorted(_FIXTURE_APP_ENVS)} — no e2e fixture logins (a synthetic superadmin does not "
            "belong in a real environment)"
        )
    else:
        # Fixture tenant first: the org-admin login carries its id as an FK.
        created["fixture_org"] = _seed_fixture_org(session)
        created["e2e_admin"] = _seed_fixture_login(
            session,
            email=os.environ.get("E2E_ADMIN_EMAIL", _DEFAULT_E2E_ADMIN_EMAIL),
            password_var="E2E_ADMIN_PASSWORD",
            superadmin=True,
        )
        created["e2e_nonsuperadmin"] = _seed_fixture_login(
            session,
            email=os.environ.get("E2E_NONSUPERADMIN_EMAIL", _DEFAULT_E2E_NONSUPERADMIN_EMAIL),
            password_var="E2E_NONSUPERADMIN_PASSWORD",
            superadmin=False,
        )
        # `superadmin=False` is the fixture's subject (see the constant block). Its binding comes after the roles.
        created["e2e_orgadmin_b"] = _seed_fixture_login(
            session,
            email=os.environ.get("E2E_ORGADMIN_B_EMAIL", _DEFAULT_E2E_ORGADMIN_B_EMAIL),
            password_var="E2E_ORGADMIN_B_PASSWORD",
            superadmin=False,
            org_id=FIXTURE_ORG_B_ID,
        )
        # A real org gets its Everyone group at create (`org_create.py::create_org_row`); the fixture tenant gets
        # it here, with its admin as a member, so a tenant's view of Everyone can be tried locally.
        created["fixture_org_everyone"] = _seed_fixture_everyone(
            session, FIXTURE_ORG_B_ID, os.environ.get("E2E_ORGADMIN_B_EMAIL", _DEFAULT_E2E_ORGADMIN_B_EMAIL)
        )

    # A count: the catalog rows arrive together.
    created["permissions"] = _seed_permissions(session)
    # After the catalog its expansion names; a count, 0 once converged.
    created["roles"] = _seed_roles(session)
    # The bindings, after the roles they name; the org-admin's and e2e admin's behind the same APP_ENV gate
    # as their logins.
    # Each superadmin owns its first seat's org and, explicitly, org zero (the same row on a fresh database).
    superadmins = [admin_email]
    if fixture_env is not None:
        superadmins.append(os.environ.get("E2E_ADMIN_EMAIL", _DEFAULT_E2E_ADMIN_EMAIL))
    created["superadmin_bindings"] = sum(bind_superadmin(session, email) for email in superadmins)
    if fixture_env is not None:
        created["e2e_orgadmin_b_binding"] = _bind_org_role(
            session,
            email=os.environ.get("E2E_ORGADMIN_B_EMAIL", _DEFAULT_E2E_ORGADMIN_B_EMAIL),
            role=ORGADMIN_B_ROLE,
        )
    # A count; 0 on every run after the first.
    created["orgs"] = _seed_orgs(session)
    # After the orgs, so it sees any just created.
    created["app_settings"] = _seed_app_settings(session)
    return created


def bootstrap(session, *, ensure: bool = False, profile: str | None = None) -> bool:
    """The once-per-env bootstrap, plus whatever `profile` asks for on top of it.

    The marker governs the bootstrap only; a `--profile` always runs (else `./dc seed --dev` would be a
    no-op on every bootstrapped stack). A bare `python -m app.seed` on a bootstrapped database writes nothing.
    """
    marker = session.get(SystemState, SEED_MARKER_KEY)
    if marker is not None and not ensure:
        print(f"seed: already bootstrapped ({marker.value.get('at', '?')}); skipping.")
        _governance_packs(session)
        _run_profile(session, profile)
        return False
    created = seed(session)
    # Bools print created/exists; counts print the number. Gate skips read as "exists"; the printed skip
    # line carries that distinction.
    print("seed: " + ", ".join(
        f"{k}={('created' if v else 'exists') if isinstance(v, bool) else v}"
        for k, v in created.items()
    ))
    _governance_packs(session)
    if marker is None:
        session.add(SystemState(
            key=SEED_MARKER_KEY,
            value={"at": datetime.now(timezone.utc).isoformat()},
        ))
    _run_profile(session, profile)
    return True


def _governance_packs(session) -> None:
    """The managed compliance packs (`app/governance/seed_packs.py`), on EVERY boot — the bootstrapped
    early return included: a new pack version ships in an image, and every deployed CP boots down that path. One
    greppable line; an invalid or failing pack reads `error: <reason>` there and the boot goes on. None in an edition
    without compliance."""
    if not edition_features.enabled("compliance"):
        return
    from app.governance import seed_packs

    results = seed_packs.seed(session)
    print("seed: governance_packs " + ", ".join(f"{k}={v}" for k, v in results.items()))


def _run_profile(session, profile: str | None) -> None:
    """Run the named seed profile, or nothing when none was asked for.

    Deferred import: `app/seed_dev_profile.py` pulls in the FastAPI route graph, which the production
    boot path must not load. An unknown name refuses rather than exiting 0.
    """
    if profile is None:
        return
    from app import seed_dev_profile

    if profile not in seed_dev_profile.PROFILES:
        raise RuntimeError(
            f"unknown seed profile {profile!r} — known profiles: "
            f"{sorted(seed_dev_profile.PROFILES)}"
        )
    created = seed_dev_profile.seed_dev(session)
    # One greppable line accounting for every row the profile is responsible for.
    print(f"seed[{profile}]: " + ", ".join(f"{k}={v}" for k, v in created.items()))


def _profile_arg(argv: list[str]) -> str | None:
    """The value of `--profile <name>`, or None. Refuses the flag with nothing after it.

    Hand-parsed, not argparse: `dc` forwards `"$@"` verbatim, and argparse would start refusing
    unrelated arguments this boot step has always ignored.
    """
    if "--profile" not in argv:
        return None
    index = argv.index("--profile")
    if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
        raise RuntimeError("--profile needs a profile name (e.g. `--profile dev`)")
    return argv[index + 1]


def main(argv: list[str] | None = None) -> None:
    """The boot entry point (`python -m app.seed`, and `app.seed.main([])` in the ECS command).

    Runs under `migrate.serialized()` — the SAME schema lock `migrate.upgrade` holds — because the control
    plane boots as two ECS tasks at once (the why is on `serialized`). The task that loses the
    lock seeds AFTER the winner's seed and must find nothing to do: it does, because this seed is what
    every boot already runs against a bootstrapped database (the `SEED_MARKER_KEY` early return in
    `bootstrap`, and `ON CONFLICT DO NOTHING` / read-then-insert on every row under `--ensure`).

    `migrate` is the top-level module beside the `app` package: both boot commands run from the container's
    `/app` (`WORKDIR`), which `python -c` and `python -m` put on `sys.path`, and pytest prepends `apps/api`
    (`app/schema_head.py` reaches it the same way). Imported here, not at module level, so importing
    `app.seed` for its constants (tests, `seed_dev_profile`) never needs it.
    """
    import migrate

    args = argv if argv is not None else sys.argv[1:]
    ensure = "--ensure" in args
    with migrate.serialized(), SessionLocal() as session:
        bootstrap(session, ensure=ensure, profile=_profile_arg(args))
        if edition.IS_CE:
            # Deferred: the community edition's org, node and pool (`app/ce_bootstrap.py`), on every boot.
            from app import ce_bootstrap

            ce_bootstrap.run(session)
        session.commit()


if __name__ == "__main__":
    main()
