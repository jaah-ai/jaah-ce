"""`python -m app.seed --profile dev` — the OPERATIONAL rows a fresh stack needs before a first epic.

`app/seed.py` deliberately seeds no projects, nodes, accounts, worktrees or tasks; this is that half for
a throwaway stack, behind `_fixture_app_env` (absent `APP_ENV` denies) plus a local-database check, and
raises elsewhere. The node token goes to stdout only: `./dc seed --dev` parses `_CREDENTIAL_LINE_PREFIX`
on the host and writes `DEV_NODE_ID`/`DEV_NODE_TOKEN`, never `NODE_ID`/`NODE_TOKEN` (owned by
`apps/jaahd/worktree_mechanics/ports.py`'s `apply_node_identity_env`). Nodes go through
`routes/nodes/register.py::_registered_node`; other rows name `org_id` explicitly. Also establishes the
Playwright suite's preconditions, and the access personas (`_access_personas`): one login per role
binding shape, so `./dc reset` leaves a stack where each role's reach can be seen by signing in as it.
And a published Live Cast (`_dev_live_cast`), whose local viewer URL it prints, so the viewer is one click away
without the operator's sign-in.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.access import Group, RoleBinding, UserGroupMembership
from app.models.agent_sessions import AgentSession
from app.models.audit import CommandAudit
from app.models.automations import Cron
from app.models.credentials import Account, Credential, ProjectCredential
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import Project, Runner
from app.models.tasks import Task
from app.models.worktrees import WORKTREE_KIND_DYNAMIC, Worktree
from app import access, agent_providers, db_credentials, edition_features, identities, project_repos, slot_allocation
from app.access_catalog import ROLES_BY_SLUG, SCOPE_ACCOUNT, SCOPE_ORG, SCOPE_PROJECT
from app.tenancy.ids import ORG_ZERO_ID
from app.plan_terms import DEFAULTS_KEY

#: The profile names the CLI accepts; a set so an unknown `--profile x` is refused by name in `app/seed.py`.
PROFILE_DEV = "dev"
PROFILES: frozenset[str] = frozenset({PROFILE_DEV})

#: The org the profile's operator-facing rows live in — projects, node, account, slots, tasks, personas — and
#: so the fixture org every API test acts in (`tests/seed.py::FIXTURE_ORG_ID`): org zero, where the
#: seeded admin's first seat is (`app/seed.py`). Org B is the fixture tenant.
DEV_ORG_ID = ORG_ZERO_ID

#: The one line `./dc seed --dev` parses: printed once, alone, grepped by prefix and split on `=`.
#: Changing either spelling means changing `dc`'s `_dc_seed_dev` too.
_CREDENTIAL_LINE_PREFIX = "DEV_NODE_CREDENTIAL"

#: The synthetic node. A DNS label, because a real node's name becomes its systemd unit and hostname.
DEV_NODE_NAME = "dev-node"
# The address a seeded node is told to dial: this stack's own api over loopback. `API_BASE_URL` follows
# `tests/conftest.py`'s convention; the default is uvicorn's in-container bind.
CP_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
#: The credential account a dispatch resolves. `oauth`, not `api_key`: the api-key arm's DB check
#: (`ck_accounts_api_key_has_credential`) requires real key material. Short because the slug renders in a
#: fixed 52px lead cell (`pages/credentials/Accounts.tsx`).
DEV_ACCOUNT_SLUG = "dev"
#: How many static slots the fixture seeds for `acme` on the dev node (its `worktrees` rows, pinned as the pair's
#: assignment override by `_dev_slots`): a pool, still readable.
DEV_SLOT_COUNT = 6

#: The database hosts this fixture accepts — the second half of its gate. `APP_ENV` describes the
#: process (compose pins `local`), while `DATABASE_URL` chooses the target; the `awsdb` profile points a
#: local api at a deployed RDS, where this would mint a real node credential. `host.docker.internal`
#: (the awsdb bridge) is deliberately absent. Imported, shared with `app/fixture_db.py`'s guard.
_LOCAL_DB_HOSTS: frozenset[str] = db_credentials.LOCAL_DB_HOSTS

#: The two fixture projects, and every delivery field a dispatch reads. Names and count are the
#: Playwright suite's requirement (`tasks-form-no-gates.spec.ts` needs `acme-ops` and a switcher); `acme`
#: sorts first as the create form's default. `repo_url` is set because slot paths derive from
#: `owner/repo` (`pool_slots.slot_path`); without it slots name no directory.
_DEV_PROJECTS: tuple[tuple[str, str], ...] = (
    ("acme", "https://github.com/example/acme"),
    ("acme-ops", "https://github.com/acme/acme-ops"),
)
#: The delivery columns every fixture project carries, spelled once. They equal the DB defaults, but a
#: row that merely inherited them looks unfinished, which `lease_drain` skips.
_DEV_PROJECT_DELIVERY: dict[str, object] = {
    "integration_branch": "develop",
    "branch_prefix": "",
    "gate_quick_cmd": "./dc check quick",
    "gate_full_cmd": "./dc check full",
    # Empty is the configured value: `stack_ops.handle_stack` fails closed on a blank command with a clear
    # sentence, which beats `docker: not found` from a node container with no docker socket. Gate commands
    # are unaffected.
    "stack_up_cmd": "",
    "stack_down_cmd": "",
}

#: The epics the board opens on, as `(title, epic state, the three analysis children's states)`, in
#: `_EPIC_ANALYSIS_CHILDREN` order; `routes/tasks/create.py::_create_analysis_children` owns the shape.
#: The first row is `draft` so an operator can press Send to build; the rest give the board, filters
#: and rollup more than one state to render.
_DEV_EPICS: tuple[tuple[str, str, tuple[str, str, str]], ...] = (
    ("Sample epic — press Send to build to start it", "draft", ("draft", "draft", "draft")),
    ("Retire the legacy export path", "active", ("done", "active", "draft")),
    ("Console: filterable audit timeline", "active", ("done", "done", "ready")),
    ("Node onboarding one-liner", "done", ("done", "done", "done")),
    ("Billing: per-slot metering", "active", ("done", "awaiting_response", "draft")),
    ("Mobile shell scroll rework", "paused", ("done", "paused", "paused")),
    ("Session transcript archive", "active", ("done", "system_failed", "draft")),
)

#: Standalone `coder` tasks — what an operator files directly — so the board renders both shapes.
_DEV_STANDALONE: tuple[tuple[str, str], ...] = (
    ("Fix the stale port banner on /worktrees", "ready"),
    ("Tighten the credential owner filter", "ready"),
    ("Drop the retired NATS env keys", "done"),
    ("Re-tag the compliance stub rows", "done"),
    ("Console: honour the terminal resize event", "active"),
    ("Investigate the slow /api/tasks list query", "active"),
    ("Answer the open question on the port grid", "awaiting_response"),
    ("Rework the node detail health card", "draft"),
    ("Prune the dead worktree probe columns", "draft"),
    ("Backfill the session transcript manifests", "system_failed"),
    ("Split the oversized dispatch composer", "paused"),
)

#: The access personas, all in `DEV_ORG_ID`: `(email, role slug, scope type, scope name)`. The scope
#: name is a `_DEV_PROJECTS` name, `DEV_ACCOUNT_SLUG`, or None for the org itself. The viewer is on
#: `acme-ops` and the maintainer/developer on `acme`, so their reach visibly differs. An account binding
#: confers `accounts:view` on that account whatever the role (`app/access.py`), so the least role is named.
#: The persona holding two grants on the org (`_DEV_PERSONAS`, `_DEV_GROUPS`).
_DEV_TWO_GRANTS_EMAIL = "dev-two-grants@cockpit.local"
_DEV_PERSONAS: tuple[tuple[str, str, str, Optional[str]], ...] = (
    ("dev-owner@cockpit.local", "org_owner", SCOPE_ORG, None),
    ("dev-maintainer@cockpit.local", "project_maintainer", SCOPE_PROJECT, "acme"),
    ("dev-developer@cockpit.local", "project_developer", SCOPE_PROJECT, "acme"),
    ("dev-viewer@cockpit.local", "project_viewer", SCOPE_PROJECT, "acme-ops"),
    ("dev-account-user@cockpit.local", "project_viewer", SCOPE_ACCOUNT, DEV_ACCOUNT_SLUG),
    # Two grants on the org at once: this direct Org Owner, and Org Admin through `DEV_ADMINS_GROUP`.
    (_DEV_TWO_GRANTS_EMAIL, "org_owner", SCOPE_ORG, None),
)
#: The group persona: a member with no binding of its own, reaching `acme-ops` through the group alone.
DEV_VIEWERS_GROUP = "Dev Viewers"
_DEV_GROUP_MEMBER_EMAIL = "dev-group-member@cockpit.local"
#: The group the two-grant persona holds Org Admin through.
DEV_ADMINS_GROUP = "Dev Admins"
#: Each dev group: `(name, its one member, (role slug, scope type, scope name))`.
_DEV_GROUPS: tuple[tuple[str, str, tuple[str, str, Optional[str]]], ...] = (
    (DEV_VIEWERS_GROUP, _DEV_GROUP_MEMBER_EMAIL, ("project_viewer", SCOPE_PROJECT, "acme-ops")),
    (DEV_ADMINS_GROUP, _DEV_TWO_GRANTS_EMAIL, ("org_admin", SCOPE_ORG, None)),
)
#: The developer persona owns the personal credential; the attached one sits in `acme`'s pool, unowned.
_DEV_PERSONAL_CREDENTIAL_OWNER = "dev-developer@cockpit.local"
_DEV_ATTACHED_CREDENTIAL = ("dev-acme-pool-credential", "acme")
_DEV_PERSONAL_CREDENTIAL = "dev-developer-personal-credential"
#: The password every persona login takes: the non-superadmin fixture's, which the local stack and CI
#: already set (the compose stack, `.env.example`). No literal: `app/seed.py` holds none.
_PERSONA_PASSWORD_VAR = "E2E_NONSUPERADMIN_PASSWORD"

#: The Live Cast fixture (`_dev_live_cast`): a tenant org of its own — the operator's org can hold no Live Cast
#: (`live_cast/entitlement.py`) — given the product by a comp, one project, and a board in the states a viewer is
#: shown (`live_cast/visibility.py::WITHHELD_STATES` hides the rest). Titles are distinct from `_DEV_STANDALONE`'s:
#: `_dev_tasks` dedupes by title alone.
DEV_LIVE_ORG_SLUG = "dev-live"
DEV_LIVE_PROJECT = "live-demo"
_DEV_LIVE_TASKS: tuple[tuple[str, str], ...] = (
    ("Live: ship the public board snapshot", "done"),
    ("Live: seed a local Live Cast", "done"),
    ("Live: page the crawlable board", "ready"),
    ("Live: word states as the board chips do", "ready"),
    ("Live: keep the snapshot until the board lands", "awaiting_response"),
)

#: The isolation probe subjects, read by the census below (each writer spells its own subject).
#: Naming is `<org>-probe-<subject>`, what `tenant-isolation.spec.ts` matches on; `audit` and
#: `admin-users` follow the spec's spelling, not the table's.
_PROBE_SUBJECTS: tuple[str, ...] = (
    "nodes", "projects", "tasks", "worktrees", "accounts",
    "credentials", "sessions", "audit", "admin-users", "groups",
)
#: What every probe row's `description` says, so an operator who finds one knows what it is.
_PROBE_DESCRIPTION = "e2e isolation probe"
def _probe_orgs() -> tuple[tuple[str, uuid.UUID], ...]:
    """`(prefix, org_id)` for the two probe tenants.

    Org A is `DEV_ORG_ID`, the operator's own org (whose rows the org-admin must not see, and every one of
    which the operator must render: `tenant-isolation.spec.ts` reads a missing org A row as an absence
    proving emptiness, not isolation); org B is `app/seed.py`'s fixture second tenant. Imported at call time:
    `app.seed` imports this module."""
    from app.seed import FIXTURE_ORG_B_ID

    return (("org-a", DEV_ORG_ID), ("org-b", FIXTURE_ORG_B_ID))


def _membership_id(session, email: str):
    """The id of the fixture login's membership (`identities.first_membership`), or None."""
    member = identities.first_membership(session, email)
    return member.id if member is not None else None


def _probe_name(org_prefix: str, subject: str) -> str:
    """`<org>-probe-<subject>` — the greppable name `tenant-isolation.spec.ts` locates rows by."""
    return f"{org_prefix}-probe-{subject}"


def _credential_id(name: str) -> uuid.UUID:
    """A seeded credential's fixed id, derived from the name it is seeded with: a re-seed finds the row by
    it, never by name (a credential's name is its owner's to change)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"jaah:seed-dev:credential:{name}")


def _seeded_credential(session, name: str, org_id: uuid.UUID) -> Optional[Credential]:
    """The seeded credential `name` in `org_id`, by its fixed id (`_credential_id`) — never by name: a dev DB
    seeded before the fixed ids gains a second copy on re-seed; `./dc reset` clears it."""
    return session.scalar(
        select(Credential).where(Credential.id == _credential_id(name), Credential.org_id == org_id))


# ── The entry point ─────────────────────────────────────────────────────────────────────────────────


def seed_dev(session) -> dict:
    """Every row `--profile dev` is responsible for. Returns a `{step: count}` summary, like `seed()`.

    Idempotent like `seed()`: each step reports what this call inserted, except the node credential,
    which is re-minted every run so `./dc seed --dev` can always repair a cleared `.env`.

    Opens the operator scope itself: the read seam is enforcing, so unbound idempotence checks would
    answer `None` and every INSERT would hit its unique index; `seed()`'s escape has closed by now. The
    escape is re-entrant. Fleet-wide by design: the probes span two tenants, and every row names its org."""
    from app.seed import _fixture_app_env
    from app.tenancy import seam

    if _fixture_app_env() is None:
        raise RuntimeError(
            f"--profile {PROFILE_DEV} refuses to run: APP_ENV is "
            f"{os.environ.get('APP_ENV') or '<unset>'!r}, and this profile creates a synthetic node "
            "holding a real credential, a pool of slots and tens of synthetic tasks — rows that belong "
            "only in a throwaway environment (app/seed.py::_FIXTURE_APP_ENVS is the permitted set, and "
            "an ABSENT APP_ENV denies)."
        )
    _refuse_a_non_local_database(session)

    created: dict[str, object] = {}
    with seam.operator_scope():
        projects = _dev_projects(session, created)
        node = _dev_node(session, created)
        account = _dev_account(session, created)
        _dev_slots(session, node, projects["acme"], created)
        _dev_tasks(session, projects["acme"], created)
        _dev_runners_and_sessions(session, node, account, projects["acme"], created)
        _isolation_probes(session, created)
        _isolation_binding(session, created)
        _e2e_fixture_groups(session, created)
        _zero_grant(session, created)
        _access_personas(session, created)
        _board_preferences(session, created)
        _dev_live_cast(session, created)
        _enable_pipeline_cron(session, created)
        _plan_switches_on(session, created)
        # Last, and read-only: an inserted count is not proof the rows are there (rejected, wrong tenant, unflushed).
        _census(session, created)
    return created


def _refuse_a_non_local_database(session) -> None:
    """The other half of the gate: refuse a session that is not pointed at a stack-local database.

    `APP_ENV` says what the process declares (compose pins `local`); `DATABASE_URL` says what it writes
    to, and the `awsdb` profile points a local api at a deployed RDS. Running inside the api container
    makes that redirect reachable.

    Fails closed on an unrecognised shape, hostless URL included, naming what to change."""
    # Off the engine's parsed `url`, never `inspect()`, which connects — the gate must refuse without a
    # reachable database.
    host = (session.get_bind().url.host or "").lower()
    if host in _LOCAL_DB_HOSTS:
        return
    raise RuntimeError(
        f"--profile {PROFILE_DEV} refuses to run: APP_ENV says {os.environ.get('APP_ENV')!r}, but this "
        f"session is connected to host {host or '<none>'!r} rather than a stack-local database "
        f"({sorted(_LOCAL_DB_HOSTS)}). APP_ENV describes the PROCESS and DATABASE_URL chooses the "
        "TARGET, so the two can disagree — the `awsdb` compose profile exists to make them disagree. "
        "This profile mints a node credential and converges the delivery columns of any project named "
        "`acme` or `acme-ops`, so a database it cannot identify as throwaway is refused rather than "
        "seeded."
    )


# ── Projects ────────────────────────────────────────────────────────────────────────────────────────


def _dev_projects(session, created: dict) -> dict[str, Project]:
    """Ensure the two fixture projects."""
    rows: dict[str, Project] = {}
    inserted = 0
    for name, repo_url in _DEV_PROJECTS:
        row = session.scalar(select(Project).where(Project.name == name, Project.org_id == DEV_ORG_ID))
        # The repository columns are the primary row's projection (`app/project_repos.py`), never set here.
        delivery = {k: v for k, v in _DEV_PROJECT_DELIVERY.items() if k not in project_repos.PROJECTED}
        repo = {"repo_url": repo_url, "integration_branch": _DEV_PROJECT_DELIVERY["integration_branch"]}
        if row is None:
            row = Project(name=name, org_id=DEV_ORG_ID, **delivery)
            session.add(row)
            session.flush()
            inserted += 1
        else:
            # Converge, don't recreate: operator edits to governance fields stay; only the delivery columns the
            # fixture promises are re-asserted.
            for field, value in delivery.items():
                setattr(row, field, value)
            if (row.repo_url or "").strip():
                repo = {"integration_branch": repo["integration_branch"]}
        project_repos.upsert_primary(session, row, repo, None)
        rows[name] = row
    created["dev_projects"] = inserted
    return rows


# ── The node, its credential, and the line the host reads ───────────────────────────────────────────


def _dev_node(session, created: dict) -> Node:
    """Register (or find) the dev node, re-issue its credential, and PRINT the one line `dc` parses.

    Through `_registered_node`, the single allow-listed `nodes` insert site: `org_id` required and the
    credential minted in the insert's transaction. `user=None`: a seed has no operator to attribute.

    `CP_BASE_URL` is this stack's loopback; `require_bare_origin` admits a loopback host over http."""
    from app import node_platform
    from app.node_bootstrap import provisioned_jaah_home
    from app.node_credentials import issue as issue_credential
    from app.routes.nodes.register import _registered_node

    row = session.scalar(select(Node).where(Node.name == DEV_NODE_NAME, Node.org_id == DEV_ORG_ID))
    if row is None:
        row, credential = _registered_node(
            session, None, DEV_NODE_NAME, cp_base_url=CP_BASE_URL, org_id=DEV_ORG_ID,
            description=(
                "The local dev fixture's node (app/seed_dev_profile.py). Registered only where APP_ENV "
                "is local/ci/test; no machine serves it."
            ),
        )
        token = credential.token
        created["dev_node"] = "created"
    else:
        # Re-issued every run: the plaintext exists only at mint (see the docstring).
        token = issue_credential(session, row)
        # Back to the home the container mounts, should the row hold another.
        row.jaah_home = provisioned_jaah_home(node_platform.OS_UBUNTU)
        created["dev_node"] = "reissued"
    # The whole host-side contract, alone on one line. `print`, not the logger, whose prefix would break
    # the host's start-of-line match.
    print(f"{_CREDENTIAL_LINE_PREFIX} DEV_NODE_ID={row.id} DEV_NODE_TOKEN={token}")
    return row


def _dev_account(session, created: dict) -> Account:
    """Ensure the one credential account a dispatch would resolve.

    `node_id` stays NULL (unpinned, usable by any node), so a second local node can use it too."""
    row = session.scalar(select(Account).where(Account.slug == DEV_ACCOUNT_SLUG))
    if row is not None:
        created["dev_account"] = "exists"
        return row
    row = Account(
        slug=DEV_ACCOUNT_SLUG,
        name="Dev fixture account",
        description=(
            "The local dev fixture's credential account (app/seed_dev_profile.py). It holds no "
            "credential material — `auth_kind='oauth'` materialises nothing here."
        ),
        org_id=DEV_ORG_ID,
    )
    session.add(row)
    session.flush()
    created["dev_account"] = "created"
    return row


def _dev_slots(session, node: Node, project: Project, created: dict) -> None:
    """Seed the static slots for `project` on `node`: `DEV_SLOT_COUNT` `worktrees` rows, then the pair's
    assignment pinned to that count so `converge` keeps them.

    The path is derived (`resolve_clone` + `slot_path`), never typed, so the port grid can reason about
    it. Every slot is `free`: pre-held slots would leave the drain nowhere to place a first epic."""
    from app.node_clone import resolve_clone
    from app.routes.pool_slots import slot_path
    from app.slot_title import default_name

    clone = resolve_clone(node.jaah_home, project.id)
    if not clone:
        raise RuntimeError(
            f"cannot derive a clone path for project {project.name!r} on node {node.name!r} — "
            "`node_clone.resolve_clone` needs the node's `jaah_home`, "
            "and this fixture sets it (app/seed_dev_profile.py::_DEV_PROJECTS)"
        )
    existing = set(session.scalars(
        select(Worktree.number).where(
            Worktree.node_id == node.id, Worktree.project_id == project.id
        )
    ).all())
    inserted = 0
    for number in range(1, DEV_SLOT_COUNT + 1):
        if number in existing:
            continue
        session.add(Worktree(
            node_id=node.id,
            # Explicit: a NULL-org slot is in no org's pool (strict equality in `slot_state.lease`), permanently
            # unleasable.
            org_id=node.org_id,
            project_id=project.id,
            number=number,
            path=slot_path(clone, number),
            # Through the helper: `slot_title.stored_opinion` recognises exactly this string as "not renamed".
            name=default_name(project.name, number),
        ))
        inserted += 1
    # The pair's assignment override holds what was seeded, so the assignment sweep keeps these slots.
    slot_allocation.pin_to_declared(session, node.id, project.id)
    created["dev_slots"] = inserted


# ── The volume: epics, their children, standalone tasks ─────────────────────────────────────────────


def _dev_tasks(session, project: Project, created: dict) -> None:
    """Seed the board: `_DEV_EPICS` with their analysis children, plus `_DEV_STANDALONE` top-level rows.

    The recipe's values are imported but `routes/tasks/create.py::_create_analysis_children` is not called:
    it builds children without `org_id`, relying on a request-bound org the write seam stamps. Under
    `operator_scope()` nothing is stamped and `tasks.org_id` is NOT NULL; `seam.bind()` needs an
    `async def`, and `seam.bind_from()` will not narrow an `OPERATOR` session. Only the loop is copied.

    States are applied after the children are built (`draft` is the recipe's contract). Idempotent on
    title: a re-run skips an existing epic whole, so it never undoes the pipeline's progress."""
    from app.routes.tasks.policy import _DISPATCH_COMPOSED_BODY, PERSONA_CODER, PERSONA_EPIC
    from app.routes.tasks.create import _EPIC_ANALYSIS_CHILDREN
    from app.models.tasks import TaskDependency
    from app.task_title import derived_child_title

    author_id = _bootstrap_admin_id(session)
    epics = 0
    children = 0
    for title, epic_state, child_states in _DEV_EPICS:
        if session.scalar(select(Task.id).where(Task.title == title)) is not None:
            continue
        epic = Task(
            project_id=project.id,
            title=title,
            body=_epic_body(title),
            persona=PERSONA_EPIC,
            state=epic_state,
            created_by_id=author_id,
            org_id=DEV_ORG_ID,
        )
        session.add(epic)
        session.flush()
        epics += 1
        prior_child_ids: list[int] = []
        for persona, state in zip(_EPIC_ANALYSIS_CHILDREN, child_states, strict=True):
            child = Task(
                project_id=epic.project_id,
                parent_task_id=epic.id,
                hidden=epic.hidden,
                title=derived_child_title(persona, epic.title),
                body=_DISPATCH_COMPOSED_BODY,
                is_plannable=False,
                state=state,
                persona=persona,
                priority=epic.priority,
                created_by_id=author_id,
                org_id=DEV_ORG_ID,
            )
            session.add(child)
            session.flush()
            # Fan-in, not a chain: each child depends on every earlier child, as `_create_analysis_children` builds.
            for prior_id in prior_child_ids:
                session.add(TaskDependency(task_id=child.id, depends_on_task_id=prior_id))
            prior_child_ids.append(child.id)
            children += 1
    standalone = 0
    for title, state in _DEV_STANDALONE:
        if session.scalar(select(Task.id).where(Task.title == title)) is not None:
            continue
        session.add(Task(
            project_id=project.id,
            title=title,
            body=_standalone_body(title),
            persona=PERSONA_CODER,
            state=state,
            created_by_id=author_id,
            org_id=DEV_ORG_ID,
        ))
        standalone += 1
    session.flush()
    created["dev_epics"] = epics
    created["dev_epic_children"] = children
    created["dev_tasks"] = standalone


def _epic_body(title: str) -> str:
    """An epic's prose. Says what it is, so nobody mistakes a fixture row for real work in flight."""
    return (
        f"{title}\n\n"
        "Seeded by `./dc seed --dev` (app/seed_dev_profile.py) so a fresh stack's console has an epic "
        "to open, filter and roll up. Not real work — delete it freely."
    )


def _standalone_body(title: str) -> str:
    """A top-level task's prose, with the same disclaimer and for the same reason."""
    return (
        f"{title}\n\n"
        "Seeded by `./dc seed --dev` (app/seed_dev_profile.py). Not real work — delete it freely."
    )


def _bootstrap_admin_id(session) -> Optional[uuid.UUID]:
    """The seeded operator's id, so the fixture rows are attributable to somebody on the board.

    `None` is a real answer (`created_by_id` is nullable; `ADMIN_EMAIL` may have changed)."""
    from app.seed import _DEFAULT_ADMIN_EMAIL

    email = os.environ.get("ADMIN_EMAIL", _DEFAULT_ADMIN_EMAIL)
    return _membership_id(session, email)


def _dev_runners_and_sessions(
    session, node: Node, account: Account, project: Project, created: dict
) -> None:
    """Give `/nodes` and Audit > Sessions something to draw.

    Both tables are observations nothing acts on, so invented rows are safe. Transcript statuses are
    spread so the degraded states render; `uploaded` is absent
    because `ck_sessions_uploaded_names_a_manifest` needs a manifest key."""
    now = datetime.now(timezone.utc)
    default = agent_providers.DEFAULT_PROVIDER
    runner_shapes = (
        (default, "active", 1), (default, "active", 2), (default, "idle", 3),
        (default, "active", 4), (default, "idle", 5), (default, "gone", 6),
    )
    inserted_runners = 0
    for kind, status, number in runner_shapes:
        cwd = f"{node.jaah_home}/{project.name}/{number}"
        if session.scalar(select(Runner.id).where(Runner.node_id == node.id, Runner.cwd == cwd)) is not None:
            continue
        session.add(Runner(
            node_id=node.id,
            org_id=node.org_id,
            pid=20000 + number,
            cwd=cwd,
            kind=kind,
            worktree_path=cwd,
            branch=f"fleet/task-{1000 + number}",
            status=status,
            last_seen=now - timedelta(minutes=number),
            account_id=account.id,
        ))
        inserted_runners += 1
    created["dev_runners"] = inserted_runners

    session_shapes = (
        ("ended", "normal", "pending"), ("ended", "normal", "failed"),
        ("ended", "idle_timeout", "gave_up"), ("ended", "abandoned", "unavailable"),
        # `reconstructed` pairs with `unknown` (see `agent_sessions.END_REASON_RECONSTRUCTED`); no DB check
        # enforces it, so the fixture must not teach a shape the writer never produces.
        ("unknown", "reconstructed", "pending"),
        ("ended", "normal", "pending"), ("running", None, "pending"),
        ("ended", "normal", "failed"), ("ended", "normal", "not_applicable"),
        ("ended", "idle_timeout", "pending"), ("running", None, "pending"),
    )
    inserted_sessions = 0
    for index, (status, end_reason, transcript_status) in enumerate(session_shapes, start=1):
        # Stable id: `uq_sessions_agent_session_id` makes this converge.
        agent_session_id = f"dev-fixture-session-{index:02d}"
        if session.scalar(
            select(AgentSession.id).where(AgentSession.agent_session_id == agent_session_id)
        ) is not None:
            continue
        # Running sessions are minutes old, ended ones hours: the Sessions page alarms on running rows older
        # than a few hours.
        started = now - (timedelta(minutes=7 * index) if status == "running" else timedelta(hours=index))
        session.add(AgentSession(
            name=agent_session_id,
            description="Seeded by ./dc seed --dev (app/seed_dev_profile.py).",
            agent_session_id=agent_session_id,
            node_id=node.id,
            account_id=account.id,
            project_id=project.id,
            org_id=node.org_id,
            started_at=started,
            ended_at=None if status == "running" else started + timedelta(minutes=17),
            status=status,
            end_reason=end_reason,
            transcript_status=transcript_status,
            cwd=f"{node.jaah_home}/{project.name}/{(index % DEV_SLOT_COUNT) + 1}",
            branch=f"fleet/task-{1000 + index}",
            model="opus",
        ))
        inserted_sessions += 1
    created["dev_sessions"] = inserted_sessions


# ── The org-isolation probes ────────────────────────────────────────────────────────────────────────


def _isolation_probes(session, created: dict) -> None:
    """One named row per subject in BOTH fixture tenants, so an absence assertion has something to be
    absent OF.

    `tenant-isolation.spec.ts` asserts this tenant's row shows and the other's does not; the second
    half is worthless against a nonexistent row. `nodes` probes go through `_registered_node` (the one
    door). `app_settings` is not probed: one row per org by construction."""
    from app.routes.nodes.register import _registered_node
    from app.routes.tasks.policy import PERSONA_CODER

    e2e_admin_id = _fixture_admin_id(session)
    counts = 0
    for org_prefix, org_id in _probe_orgs():
        node = _probe_node(session, org_prefix, org_id, _registered_node, CP_BASE_URL)
        project = _probe_project(session, org_prefix, org_id)
        task = _probe_task(session, org_prefix, org_id, project, PERSONA_CODER)
        counts += _probe_worktree(session, org_prefix, org_id, node, project, task)
        counts += _probe_account(session, org_prefix, org_id)
        counts += _probe_credential(session, org_prefix, org_id, e2e_admin_id)
        counts += _probe_session(session, org_prefix, org_id)
        counts += _probe_audit(session, org_prefix, org_id)
        counts += _probe_admin_user(session, org_prefix, org_id)
        counts += _probe_group(session, org_prefix, org_id)
    created["isolation_probes"] = counts


def _probe_node(session, org_prefix: str, org_id: uuid.UUID, registered_node, cp_base_url: str) -> Node:
    """The `<org>-probe-nodes` row, registered through the one door."""
    name = _probe_name(org_prefix, "nodes")
    row = session.scalar(select(Node).where(Node.name == name, Node.org_id == org_id))
    if row is not None:
        return row
    row, _credential = registered_node(
        session, None, name, cp_base_url=cp_base_url, org_id=org_id, description=_PROBE_DESCRIPTION,
    )
    return row


def _probe_project(session, org_prefix: str, org_id: uuid.UUID) -> Project:
    """The `<org>-probe-projects` row.

    No `description` (the table has no such column) and no `port_block` (unique per org; could collide)."""
    name = _probe_name(org_prefix, "projects")
    row = session.scalar(select(Project).where(Project.name == name, Project.org_id == org_id))
    if row is None:
        row = Project(name=name, org_id=org_id)
        session.add(row)
        session.flush()
    return row


def _probe_task(session, org_prefix, org_id, project: Project, persona: str) -> Task:
    """The `<org>-probe-tasks` row, `draft` deliberately: a `ready` task would be dispatched by a drain."""
    title = _probe_name(org_prefix, "tasks")
    row = session.scalar(select(Task).where(Task.title == title))
    if row is not None:
        return row
    row = Task(
        project_id=project.id,
        title=title,
        body=_PROBE_DESCRIPTION,
        persona=persona,
        state="draft",
        org_id=org_id,
    )
    session.add(row)
    session.flush()
    return row


def _probe_worktree(session, org_prefix, org_id, node: Node, project: Project, task: Task) -> int:
    """The `<org>-probe-worktrees` row, HELD BY the probe task.

    The hold matters: a `free` slot is invisible to a scoped viewer. `task` is the only hold available
    (`user` needs an `owner_user_id`). Returns rows inserted, like every probe, so a converged re-run
    reports zero; the hold is still converged unconditionally."""
    name = _probe_name(org_prefix, "worktrees")
    row = session.scalar(select(Worktree).where(Worktree.name == name))
    inserted = 0
    if row is None:
        row = Worktree(
            name=name,
            description=_PROBE_DESCRIPTION,
            number=91,
            path=f"/nonexistent/e2e-probe/{name}",
            node_id=node.id,
            project_id=project.id,
            org_id=org_id,
            # Dynamic: the probe project sizes no pool, and a held dynamic worktree on an assignment of zero
            # statics is what the assignment model converges to and then leaves alone.
            kind=WORKTREE_KIND_DYNAMIC,
            has_repo=False,
        )
        session.add(row)
        session.flush()
        inserted = 1
    # A folder slot, as the repo-less probe project implies: a git slot there is retired by the converge pass
    # (`slot_allocation._converge_assigned`), and the probe node has no daemon to confirm the destroy, so the
    # row sat at "Destroying" for good. A row seeded before this converges here; its old `destroy_slot` intent
    # stays, harmless, since the probe node never runs a daemon.
    if row.has_repo or row.retiring_at is not None or row.destroy_error is not None:
        row.has_repo = False
        row.retiring_at = None
        row.destroy_error = None
    if row.state != "task" or row.task_id != task.id:
        row.state = "task"
        row.task_id = task.id
        row.owner_user_id = None
    slot_allocation.pin_to_declared(session, node.id, project.id)
    return inserted


def _probe_account(session, org_prefix, org_id) -> int:
    """The `<org>-probe-accounts` row. `node_id` stays NULL — the unpinned arm of the read predicate."""
    name = _probe_name(org_prefix, "accounts")
    if session.scalar(select(Account.id).where(Account.slug == name)) is not None:
        return 0
    session.add(Account(slug=name, name=name, description=_PROBE_DESCRIPTION, org_id=org_id))
    return 1


def _probe_credential(session, org_prefix, org_id, author_id: Optional[uuid.UUID]) -> int:
    """The `<org>-probe-credentials` row, WITH ITS AUTHOR STAMPED and with no secret material.

    The stamp is mandatory: `GET /api/credentials/owners` inner-joins `created_by_id`, and the
    system-actor default breaks `credentials-filters.spec.ts`. `updated_by_id` is unread."""
    name = _probe_name(org_prefix, "credentials")
    row = _seeded_credential(session, name, org_id)
    inserted = 0
    if row is None:
        row = Credential(id=_credential_id(name), name=name, type="generic_api_key",
                         description=_PROBE_DESCRIPTION, org_id=org_id)
        session.add(row)
        session.flush()
        inserted = 1
    if author_id is None:
        # Loud: an unstamped credential fails `credentials-filters.spec.ts` with a message about owners.
        print(
            f"seed[dev]: no E2E_ADMIN_EMAIL row to stamp {name!r} with — the credential owner roster "
            "will carry the system actor as a second owner"
        )
    elif row.created_by_id != author_id:
        row.created_by_id = author_id
    return inserted


def _probe_session(session, org_prefix, org_id) -> int:
    """The `<org>-probe-sessions` row. `node_id` stays NULL: that arm of `visible_node_ref_clause` shows the
    row without granting a node."""
    name = _probe_name(org_prefix, "sessions")
    if session.scalar(select(AgentSession.id).where(AgentSession.agent_session_id == name)) is not None:
        return 0
    session.add(AgentSession(
        name=name, description=_PROBE_DESCRIPTION, agent_session_id=name, status="ended", org_id=org_id,
    ))
    return 1


def _probe_audit(session, org_prefix, org_id) -> int:
    """The `<org>-probe-audit` row in `command_audit_log`.

    The probe name is in `command_type` because `CommandOut` does not serve `actor`. `args` has no
    `_test` key: `routes/portfolio.py` hides rows carrying one."""
    name = _probe_name(org_prefix, "audit")
    if session.scalar(select(CommandAudit.id).where(CommandAudit.command_type == name)) is not None:
        return 0
    session.add(CommandAudit(
        command_type=name, args={"probe": name}, status="dispatched", actor=name, org_id=org_id,
    ))
    return 1


def _probe_admin_user(session, org_prefix, org_id) -> int:
    """The `<org>-probe-admin-users` row, WITH NO `password_hash`: listed on `/directory`, never signable-in."""
    name = _probe_name(org_prefix, "admin-users")
    email = f"{name}@cockpit.local"
    if _membership_id(session, email) is not None:
        return 0
    session.add(AdminUser(
        email=email,
        description=_PROBE_DESCRIPTION,
        is_superadmin=False,
        activated_at=datetime.now(timezone.utc),
        org_id=org_id,
    ))
    return 1


def _probe_group(session, org_prefix, org_id) -> int:
    """The `<org>-probe-groups` row."""
    name = _probe_name(org_prefix, "groups")
    if session.scalar(select(Group.id).where(Group.name == name, Group.org_id == org_id)) is not None:
        return 0
    session.add(Group(name=name, description=_PROBE_DESCRIPTION, org_id=org_id))
    return 1


def _isolation_binding(session, created: dict) -> None:
    """Converge the org-admin fixture's `org_admin` binding at org B, which is all its reach over its own
    tenant's probes. `seed()` writes it at bootstrap; repeated here because a bootstrapped stack skips
    `seed()` (the marker) while this profile always runs."""
    from app.seed import _DEFAULT_E2E_ORGADMIN_B_EMAIL, ORGADMIN_B_ROLE, _bind_org_role

    created["isolation_binding"] = _bind_org_role(
        session,
        email=os.environ.get("E2E_ORGADMIN_B_EMAIL", _DEFAULT_E2E_ORGADMIN_B_EMAIL),
        role=ORGADMIN_B_ROLE,
    )


def _e2e_fixture_groups(session, created: dict) -> None:
    """Converge the e2e specs' role-carrying groups (`app/seed.py::E2E_DEVELOPERS_GROUP` and
    `E2E_COMPANY_OWNERS_GROUP`, the latter in its own tenant org, created here if missing). Here for
    `_isolation_binding`'s reason: a bootstrapped stack skips `seed()`."""
    from app.models.orgs import Org
    from app.seed import (
        E2E_COMPANY_ORG_SLUG, E2E_COMPANY_OWNERS_GROUP, E2E_DEVELOPERS_GROUP, _seed_fixture_everyone,
        _seed_fixture_group,
    )

    created["e2e_groups"] = _seed_fixture_group(session, name=E2E_DEVELOPERS_GROUP, role="project_developer")
    # The company and self-service orgs are further tenants: an edition with one org has neither.
    if not edition_features.enabled("multi_org_tenancy"):
        return
    from app.signup_plans import PRESELECTED

    company = session.scalar(select(Org).where(Org.slug == E2E_COMPANY_ORG_SLUG))
    if company is None:
        company = Org(slug=E2E_COMPANY_ORG_SLUG, name="E2E Company Setup", description="company-setup-card.spec.ts",
                      plan_key=PRESELECTED)
        session.add(company)
        session.flush()
    # Its Everyone group, as a real org's create makes one.
    _seed_fixture_everyone(session, company.id)
    created["e2e_groups"] += _seed_fixture_group(
        session, name=E2E_COMPANY_OWNERS_GROUP, role="org_owner", org_id=company.id
    )
    _e2e_self_service_org(session, created)


def _e2e_self_service_org(session, created: dict) -> None:
    """A PENDING self-service org with its owner, made as the sign-up door makes one (`app/org_create.py`:
    org, user, `OrgSignup`, owner binding, and the clickwrap the submit writes — `signup.record_submission` —
    without which the Terms gate holds the owner, `signup.terms_pending`). No password: the spec sets one over
    `PATCH /api/users/{id}`. Idempotent: an existing org is left as it is. None in an edition with one org."""
    if not edition_features.enabled("multi_org_tenancy"):
        return
    from app.models.identity import LegalAcceptance
    from app.models.orgs import ORG_PENDING, Org, OrgSignup
    from app.org_create import bind_owner, create_org_row
    from app.seed import E2E_SELF_SERVICE_ORG_SLUG, E2E_SELF_SERVICE_OWNER_EMAIL

    if session.scalar(select(Org.id).where(Org.slug == E2E_SELF_SERVICE_ORG_SLUG)) is not None:
        created["e2e_self_service"] = 0
        return
    org = create_org_row(
        session, slug=E2E_SELF_SERVICE_ORG_SLUG, name="E2E Self Service",
        description="company-setup-self-service.spec.ts", lifecycle=ORG_PENDING, self_service=True,
        plan_key=DEFAULTS_KEY,
    )
    owner = AdminUser(
        email=E2E_SELF_SERVICE_OWNER_EMAIL, role="admin", org_id=org.id,
        activated_at=datetime.now(timezone.utc),
    )
    session.add(owner)
    session.flush()
    now = datetime.now(timezone.utc)
    session.add(OrgSignup(
        org_id=org.id, owner_id=owner.id, provider="google", email=owner.email,
        signed_up_at=now, submitted_at=now,
    ))
    session.add(LegalAcceptance(
        user_id=owner.id, identity_id=owner.identity_id, legal_version="seed", terms_url="", privacy_url="",
        accepted_at=now,
    ))
    session.flush()
    bind_owner(session, owner)
    created["e2e_self_service"] = 1


# ── The zero-grant identity ─────────────────────────────────────────────────────────────────────────


def _zero_grant(session, created: dict) -> None:
    """Strip the non-superadmin fixture identity back to holding NO feature permission.

    The property is the fixture: `profile-selfservice.spec.ts` waits for `/no-access`, reachable only
    while the identity holds nothing. Refuses a superadmin (allow-all no revocation can reach). Its own
    role bindings and its group memberships (a group's bindings) are the two paths to a key."""
    from app.seed import _DEFAULT_E2E_NONSUPERADMIN_EMAIL

    email = os.environ.get("E2E_NONSUPERADMIN_EMAIL", _DEFAULT_E2E_NONSUPERADMIN_EMAIL)
    user = identities.first_membership(session, email)
    if user is None:
        print(f"seed[dev]: no {email} row — nothing to strip to zero grants (the login was skipped)")
        created["zero_grant"] = "skipped"
        return
    if user.is_superadmin:
        raise RuntimeError(
            f"refusing to strip grants from {email!r}: it is a superadmin, and the flag is an allow-all "
            "no revocation here can reach — the identity would still hold everything and this step would "
            "report success (app/seed.py seeds it with `superadmin=False`)"
        )
    removed = session.execute(
        delete(UserGroupMembership).where(UserGroupMembership.user_id == user.id)
    ).rowcount or 0
    removed += session.execute(
        delete(RoleBinding).where(RoleBinding.user_id == user.id)
    ).rowcount or 0
    # Read the end state back: a non-empty effective set means some path grants this identity, reported here
    # instead of as spec timeouts. Through `authz.effective_permissions`, the query the decision itself uses;
    # flushed first so it does not read the rows just deleted.
    from app.authz.principal import effective_permissions

    session.flush()
    still_held = effective_permissions(session, user.id)
    if still_held:
        raise RuntimeError(
            f"{email!r} still holds {still_held} after {removed} revocation(s) — this identity's whole "
            "purpose is to hold NOTHING (profile-selfservice.spec.ts waits for `/no-access`, which is "
            "reachable from no other state), and it is granted by a path this step does not clear: "
            "`user_group_memberships` and `role_bindings` are the two it deletes, so the remaining "
            "grant is coming from somewhere else"
        )
    created["zero_grant"] = removed


# ── The access personas ─────────────────────────────────────────────────────────────────────────────


def _persona_scope(session, scope_type: str, scope_name: Optional[str]) -> dict:
    """The one `role_bindings` scope column a persona's binding names, resolved by name in `DEV_ORG_ID`.

    A value of None means the named row is absent (a project or account this profile creates first)."""
    if scope_type == SCOPE_ORG:
        return {"scope_org_id": DEV_ORG_ID}
    if scope_type == SCOPE_PROJECT:
        return {"project_id": session.scalar(
            select(Project.id).where(Project.name == scope_name, Project.org_id == DEV_ORG_ID))}
    if scope_type == SCOPE_ACCOUNT:
        return {"account_id": session.scalar(
            select(Account.id).where(Account.slug == scope_name, Account.org_id == DEV_ORG_ID))}
    raise ValueError(f"no persona scope {scope_type!r}")


def _bind(session, *, role: str, scope: dict, user_id=None, group_id=None) -> int:
    """Upsert one binding in `DEV_ORG_ID`; returns how many this call inserted (0 or 1). Refuses a scope
    that resolved to nothing: the row would fail `ck_role_bindings_one_scope` with no name attached."""
    if any(value is None for value in scope.values()):
        raise RuntimeError(f"seed[dev]: persona {role} binding names a missing scope {scope} — "
                           "the project or account this profile creates first is absent")
    return len(session.scalars(
        pg_insert(RoleBinding)
        .values(org_id=DEV_ORG_ID, user_id=user_id, group_id=group_id,
                role_id=ROLES_BY_SLUG[role].id, **scope)
        .on_conflict_do_nothing(constraint="uq_role_bindings")
        .returning(RoleBinding.id)
    ).all())


def _user_id(session, email: str) -> Optional[uuid.UUID]:
    return _membership_id(session, email)


def _access_personas(session, created: dict) -> None:
    """One login per binding shape, the dev groups, and one attached plus one personal credential.
    Every login is a member of the org's Everyone group.

    Every row is a binding, a membership or a credential: no key or grant row exists to write. Logins go
    through `app/seed.py::_seed_fixture_login` (non-superadmin, `_PERSONA_PASSWORD_VAR`), which skips one
    with a printed reason when the variable is unset; a skipped login's binding is skipped the same way.
    Idempotent: each count is what this call inserted."""
    from app.seed import _seed_fixture_login

    emails = [email for email, *_rest in _DEV_PERSONAS] + [_DEV_GROUP_MEMBER_EMAIL]
    created["dev_personas"] = sum(
        _seed_fixture_login(session, email=email, password_var=_PERSONA_PASSWORD_VAR, superadmin=False)
        for email in emails
    )
    session.flush()
    # Every member of an org belongs to its Everyone group, as a real create does (`routes/users.py`,
    # `identities.py`); `_seed_fixture_login` joins only a superadmin, so the personas join here.
    # Idempotent, and it reaches a persona row an earlier seed left outside Everyone.
    for email in emails:
        user = identities.first_membership(session, email)
        if user is not None:
            access.join_everyone(session, user)

    bindings = 0
    for email, role, scope_type, scope_name in _DEV_PERSONAS:
        user_id = _user_id(session, email)
        if user_id is None:
            print(f"seed[dev]: no {email} row — no {role} binding (the login itself was skipped)")
            continue
        bindings += _bind(session, role=role, scope=_persona_scope(session, scope_type, scope_name),
                          user_id=user_id)
    created["dev_persona_bindings"] = bindings

    group_rows = 0
    for name, member_email, (role, scope_type, scope_name) in _DEV_GROUPS:
        group = session.scalar(select(Group).where(Group.name == name, Group.org_id == DEV_ORG_ID))
        if group is None:
            group = Group(name=name, description="Dev fixture group (app/seed_dev_profile.py).", org_id=DEV_ORG_ID)
            session.add(group)
            session.flush()
        group_rows += _bind(session, role=role, scope=_persona_scope(session, scope_type, scope_name),
                            group_id=group.id)
        member_id = _user_id(session, member_email)
        if member_id is not None and session.scalar(
            select(UserGroupMembership.id).where(
                UserGroupMembership.user_id == member_id, UserGroupMembership.group_id == group.id)
        ) is None:
            session.add(UserGroupMembership(user_id=member_id, group_id=group.id, org_id=DEV_ORG_ID))
            group_rows += 1
    created["dev_group"] = group_rows

    created["dev_credentials"] = _persona_credentials(session)


def _persona_credentials(session) -> int:
    """The attached credential (in `acme`'s pool, unowned) and the personal one (owned by the developer).

    Author-stamped with the e2e smoke identity, as `_probe_credential` is: `/api/credentials/owners`
    groups by `created_by_id`, and `credentials-filters.spec.ts` reads that roster. No secret material.
    Returns rows inserted (credentials plus the pool row)."""
    author_id = _fixture_admin_id(session)
    inserted = 0

    def _credential(name: str, owner_id: Optional[uuid.UUID]) -> Credential:
        nonlocal inserted
        row = _seeded_credential(session, name, DEV_ORG_ID)
        if row is None:
            row = Credential(id=_credential_id(name), name=name, type="generic_api_key", owner_id=owner_id,
                             description="Dev fixture credential (app/seed_dev_profile.py).",
                             org_id=DEV_ORG_ID)
            session.add(row)
            session.flush()
            inserted += 1
        # Converge the owner too: deleting the owning persona SETs NULL on `owner_id` (models.Credential),
        # and a re-seeded persona is a new row, so an existing personal credential would stay the org's.
        if row.owner_id != owner_id:
            row.owner_id = owner_id
        if author_id is not None and row.created_by_id != author_id:
            row.created_by_id = author_id
        return row

    name, project_name = _DEV_ATTACHED_CREDENTIAL
    attached = _credential(name, None)
    project_id = _persona_scope(session, SCOPE_PROJECT, project_name)["project_id"]
    if session.scalar(select(ProjectCredential.credential_id).where(
        ProjectCredential.project_id == project_id, ProjectCredential.credential_id == attached.id
    )) is None:
        session.add(ProjectCredential(project_id=project_id, credential_id=attached.id, org_id=DEV_ORG_ID))
        inserted += 1

    owner_id = _user_id(session, _DEV_PERSONAL_CREDENTIAL_OWNER)
    if owner_id is None:
        print(f"seed[dev]: no {_DEV_PERSONAL_CREDENTIAL_OWNER} row — no personal credential to own")
    else:
        _credential(_DEV_PERSONAL_CREDENTIAL, owner_id)
    session.flush()
    return inserted


def _persona_census(session) -> list[str]:
    """What `_access_personas` promised and the database does not hold, for `_census` to refuse on.

    A persona whose login is absent is skipped (its password variable was unset, printed at write)."""
    missing: list[str] = []
    for email, role, scope_type, scope_name in _DEV_PERSONAS:
        user_id = _user_id(session, email)
        if user_id is None:
            continue
        clauses = [RoleBinding.user_id == user_id, RoleBinding.role_id == ROLES_BY_SLUG[role].id]
        clauses += [getattr(RoleBinding, column) == value
                    for column, value in _persona_scope(session, scope_type, scope_name).items()]
        if session.scalar(select(RoleBinding.id).where(*clauses)) is None:
            missing.append(f"binding {role} -> {email}")
    for name, member_email, (role, scope_type, scope_name) in _DEV_GROUPS:
        group_id = session.scalar(select(Group.id).where(Group.name == name, Group.org_id == DEV_ORG_ID))
        scope = [getattr(RoleBinding, column) == value
                 for column, value in _persona_scope(session, scope_type, scope_name).items()]
        if group_id is None or session.scalar(select(RoleBinding.id).where(
            RoleBinding.group_id == group_id, RoleBinding.role_id == ROLES_BY_SLUG[role].id, *scope,
        )) is None:
            missing.append(f"group binding {role} -> {name}")
        member_id = _user_id(session, member_email)
        if member_id is not None and group_id is not None and session.scalar(select(UserGroupMembership.id).where(
            UserGroupMembership.user_id == member_id, UserGroupMembership.group_id == group_id
        )) is None:
            missing.append(f"membership {member_email} -> {name}")
    name, project_name = _DEV_ATTACHED_CREDENTIAL
    attached = _seeded_credential(session, name, DEV_ORG_ID)
    if attached is None or attached.owner_id is not None or session.scalar(
        select(ProjectCredential.credential_id).where(
            ProjectCredential.credential_id == attached.id,
            ProjectCredential.project_id == _persona_scope(session, SCOPE_PROJECT, project_name)["project_id"])
    ) is None:
        missing.append(f"attached credential {name}")
    owner_id = _user_id(session, _DEV_PERSONAL_CREDENTIAL_OWNER)
    personal = _seeded_credential(session, _DEV_PERSONAL_CREDENTIAL, DEV_ORG_ID)
    if owner_id is not None and (personal is None or personal.owner_id != owner_id):
        missing.append(f"personal credential {_DEV_PERSONAL_CREDENTIAL}")
    return missing


# ── The board's own preconditions ───────────────────────────────────────────────────────────────────


def _fixture_admin_id(session) -> Optional[uuid.UUID]:
    """The e2e smoke identity's id, or None where that optional login was skipped."""
    from app.seed import _DEFAULT_E2E_ADMIN_EMAIL

    email = os.environ.get("E2E_ADMIN_EMAIL", _DEFAULT_E2E_ADMIN_EMAIL)
    return _membership_id(session, email)


#: The nameless smoke identity's name prompt, recorded closed: the console's `NAME_PROMPT_DISMISSED_STEP`
#: (`apps/web/src/lib/onboarding.ts`); the server stores step ids as opaque strings.
_NAME_PROMPT_DISMISSED_STEP = "name_prompt_dismissed"


def _board_preferences(session, created: dict) -> None:
    """Put the e2e smoke identity on a board that shows everything, with the Get-started card and the name prompt
    dismissed.

    An undismissed Get-started card (or the in-flow name prompt) shifts the board's geometry, which fails specs
    as something else. Written through `app/preferences.py`'s round-trip so the stored document keeps the API's shape, including the `notifications` block."""
    from app.preferences import UserPreferences, preferences_to_store

    # Selected, not `session.get`: the tenancy-bypass census test counts primary-key `get`s.
    from app.seed import _DEFAULT_E2E_ADMIN_EMAIL

    email = os.environ.get("E2E_ADMIN_EMAIL", _DEFAULT_E2E_ADMIN_EMAIL)
    user = identities.first_membership(session, email)
    if user is None:
        created["board_preferences"] = "skipped"
        return
    preferences = UserPreferences.from_stored(user.preferences)
    if preferences.onboarding_dismissed and _NAME_PROMPT_DISMISSED_STEP in preferences.onboarding_steps_done:
        created["board_preferences"] = "exists"
        return
    preferences.onboarding_dismissed = True
    if _NAME_PROMPT_DISMISSED_STEP not in preferences.onboarding_steps_done:
        preferences.onboarding_steps_done = [*preferences.onboarding_steps_done, _NAME_PROMPT_DISMISSED_STEP]
    user.preferences = preferences_to_store(preferences, user.preferences)
    created["board_preferences"] = "set"


# ── The census: the read-back that makes a green run falsifiable ────────────────────────────────────


#: How the census finds each probe, one entry per `_PROBE_SUBJECTS` member; each takes
#: `(session, name, org_id)`. It re-states the key rather than reusing the writer's return, so it asks
#: the database, in the expected tenant. The `credentials` entry also asserts the author stamp.
_PROBE_CENSUS: dict[str, object] = {
    "nodes": lambda session, name, org_id: session.scalar(
        select(Node.id).where(Node.name == name, Node.org_id == org_id)) is not None,
    "projects": lambda session, name, org_id: session.scalar(
        select(Project.id).where(Project.name == name, Project.org_id == org_id)) is not None,
    "tasks": lambda session, name, org_id: session.scalar(
        select(Task.id).where(Task.title == name, Task.org_id == org_id)) is not None,
    "worktrees": lambda session, name, org_id: session.scalar(
        select(Worktree.id).where(
            Worktree.name == name, Worktree.org_id == org_id, Worktree.state == "task",
            Worktree.task_id.is_not(None))) is not None,
    "accounts": lambda session, name, org_id: session.scalar(
        select(Account.id).where(Account.slug == name, Account.org_id == org_id)) is not None,
    "credentials": lambda session, name, org_id: _credential_probe_present(session, name, org_id),
    "sessions": lambda session, name, org_id: session.scalar(
        select(AgentSession.id).where(
            AgentSession.agent_session_id == name, AgentSession.org_id == org_id)) is not None,
    "audit": lambda session, name, org_id: session.scalar(
        select(CommandAudit.id).where(
            CommandAudit.command_type == name, CommandAudit.org_id == org_id)) is not None,
    "admin-users": lambda session, name, org_id: session.scalar(
        select(AdminUser.id).where(
            AdminUser.email == f"{name}@cockpit.local", AdminUser.org_id == org_id)) is not None,
    "groups": lambda session, name, org_id: session.scalar(
        select(Group.id).where(Group.name == name, Group.org_id == org_id)) is not None,
}

def _credential_probe_present(session, name: str, org_id: uuid.UUID) -> bool:
    """The `credentials` probe, present AND attributed to the identity the suite reads.

    `created_by_id.is_not(None)` would not do: its server default names the system actor. Where the e2e
    identity exists, the stamp must be it; where it does not, the row is all this can check."""
    author_id = _fixture_admin_id(session)
    row = _seeded_credential(session, name, org_id)
    if row is None:
        return False
    return row.created_by_id == author_id if author_id is not None else row.created_by_id is not None


def _census(session, created: dict) -> None:
    """Read every probe row, the org-admin's binding and the access personas back, and REFUSE if one is
    absent.

    Without it a rejected insert, wrong-tenant row or missing binding reports cheerful counts, then
    surfaces as `tenant-isolation.spec.ts` failing on empty pages. The org-admin reaches org B's probes
    through its `org_admin` binding alone (`app/seed.py::_bind_org_role`): org-wide keys see every node,
    slot, org account and org credential of its tenant, so no per-object row is needed.

    An absent fixture identity is printed, not raised: a raise rolls back the whole operator seed over a
    login the console does not need. What it refuses is its own fault: identity present, row or binding
    missing."""
    from app.access_catalog import ROLES_BY_SLUG
    from app.seed import _DEFAULT_E2E_ORGADMIN_B_EMAIL, ORGADMIN_B_ROLE

    missing: list[str] = []
    for org_prefix, org_id in _probe_orgs():
        for subject in _PROBE_SUBJECTS:
            name = _probe_name(org_prefix, subject)
            if not _PROBE_CENSUS[subject](session, name, org_id):
                missing.append(name)

    email = os.environ.get("E2E_ORGADMIN_B_EMAIL", _DEFAULT_E2E_ORGADMIN_B_EMAIL)
    user = identities.first_membership(session, email)
    if user is None:
        # Not in `missing`, per the docstring: nothing here could have created this identity.
        print(
            f"seed[dev]: no {email} row, so its org-b {ORGADMIN_B_ROLE} binding cannot exist — "
            "tenant-isolation.spec.ts's presence half will fail on empty /nodes, /worktrees, "
            "/accounts and /credentials pages until `./dc seed` mints that login "
            "(it skips one whose password variable is unset, and says so)"
        )
        created["census"] = "no-orgadmin-identity"
    elif session.scalar(
        select(RoleBinding.id).where(
            RoleBinding.user_id == user.id,
            RoleBinding.role_id == ROLES_BY_SLUG[ORGADMIN_B_ROLE].id,
            RoleBinding.scope_org_id == user.org_id,
        )
    ) is None:
        missing.append(f"binding {ORGADMIN_B_ROLE} -> {email}")

    missing += _persona_census(session)

    if missing:
        raise RuntimeError(
            f"--profile {PROFILE_DEV} seeded, then could not read back "
            f"{len(missing)} row(s)/binding(s): {', '.join(missing)}. Each one makes an isolation "
            "assertion pass VACUOUSLY or fail on an empty page in another suite, so it is refused here "
            "instead: a probe absent after its insert was rejected, landed in the wrong tenant (this "
            "census conjoins org_id), or had no dependency row to name; a `credentials` probe reported "
            "absent may exist with no author stamped, which is what the owner roster reads."
        )
    created.setdefault("census", "ok")


def _dev_live_cast(session, created: dict) -> None:
    """Publish a Live Cast of its own project (`DEV_LIVE_ORG_SLUG`, constants above) through the console's own
    path (`app/live_cast/state.py`: save Default All with no rules, publish), and print its local viewer URL: the
    dev server answers `live.localhost:<FRONTEND_PORT>` as the live host (`apps/web/vite.config.ts`). Publishing
    enqueues the evaluation the serving api runs; with no rules it publishes every non-draft task, no model
    call. Idempotent: an existing org, comp, project or task is kept, and a published row stays as it is. Skipped in
    an edition without Live Cast."""
    if not edition_features.enabled("live_cast"):
        created["dev_live_cast"] = "skipped"
        return
    from urllib.parse import urlsplit

    from app.live_cast import state
    from app.live_cast.models import DEFAULT_ALL
    from app.models.billing import OrgComp
    from app.org_create import create_org_row
    from app.routes.tasks.policy import PERSONA_CODER
    from app.signup_plans import PRESELECTED

    actor_id = _bootstrap_admin_id(session)
    if actor_id is None:
        print("seed[dev]: no bootstrap admin — no Live Cast published (it stamps the publish)")
        created["dev_live_cast"] = "skipped"
        return
    org = session.scalar(select(Org).where(Org.slug == DEV_LIVE_ORG_SLUG))
    if org is None:
        org = create_org_row(session, slug=DEV_LIVE_ORG_SLUG, name="Dev Live",
                             description="The local Live Cast (`./dc seed --dev`)", self_service=False,
                             plan_key=PRESELECTED)
    if session.scalar(select(OrgComp.id).where(OrgComp.org_id == org.id, OrgComp.kind == "live_cast")) is None:
        session.add(OrgComp(org_id=org.id, name="Dev Live Cast", kind="live_cast", quantity=1, backfill_tasks=50,
                            end_mode="never"))
    project = session.scalar(select(Project).where(Project.org_id == org.id, Project.name == DEV_LIVE_PROJECT))
    if project is None:
        project = Project(name=DEV_LIVE_PROJECT, org_id=org.id)
        session.add(project)
    session.flush()
    for title, task_state in _DEV_LIVE_TASKS:
        if session.scalar(select(Task.id).where(Task.project_id == project.id, Task.title == title)) is None:
            session.add(Task(project_id=project.id, title=title, body=_standalone_body(title), persona=PERSONA_CODER,
                             state=task_state, org_id=org.id))
    session.flush()
    row = state.ensure(session, project, actor_id)
    needs_publish = row.published_at is None
    if needs_publish:
        state.save(session, row, enabled=True, default_mode=DEFAULT_ALL, rules=[], actor_id=actor_id)
        state.publish(session, row, state.require_product(session, org.id), actor_id)
    frontend = urlsplit(os.environ.get("FRONTEND_URL", "http://localhost:3020"))
    print(f"seed[dev]: Live Cast: {frontend.scheme}://live.{frontend.netloc}/{org.slug}/{row.slug}")
    created["dev_live_cast"] = "published" if needs_publish else "exists"


def _plan_switches_on(session, created: dict) -> None:
    """Turn the plan switches a new major feature ships behind ON for every seeded org that has not
    decided its own, as dev runs: they ship off on every plan, and this stack exercises the features. A test that
    needs one off sets the org's override to False. Likewise every built-in provider cell and the SDK agent row,
    through the org's provider grid override (`plan_terms.providers_term`) — the operator org's too, whose override
    revision 202610251500 wrote from the shipped switch (off) before this seed ran. No ACP-registry row: those stay
    off until an operator turns one on."""
    from app import provider_auth_policy

    names = ("playbooks_enabled",)
    seeded_grid = provider_auth_policy.encoded(
        [*provider_auth_policy.builtin_cells(), (provider_auth_policy.SDK_ROW, provider_auth_policy.OWN)]
    )
    count = 0
    for org in session.scalars(select(Org)).all():
        for name in names:
            if getattr(org, f"{name}_override") is None:
                setattr(org, f"{name}_override", True)
                count += 1
        if org.providers_override is None or (org.id == ORG_ZERO_ID and org.providers_override != seeded_grid):
            org.providers_override = seeded_grid
            count += 1
    created["plan_switches_on"] = count


def _enable_pipeline_cron(session, created: dict) -> None:
    """Turn the `Task pipeline` system cron ON, because without it an epic cannot advance at all.

    The baseline ships it disabled; off, a started epic's children sit in `draft` for ever.

    Only this one: `Lease drain` would issue real `lease_slot` intents to a node nobody serves and fail
    the board `system_failed`. `./dc --profile node up` serves the node and enables the drain itself
    (`dc`'s `dc_node_enable_lease_drain`)."""
    row = session.scalar(select(Cron).where(Cron.activity_type == "task-pipeline", Cron.is_system.is_(True)))
    if row is None:
        # The baseline migration inserts it, so absence means unapplied migrations. Printed: the rest is still useful.
        print("seed[dev]: no 'task-pipeline' system cron row — epic fan-out will not run on this stack")
        created["pipeline_cron"] = "missing"
        return
    if row.enabled:
        created["pipeline_cron"] = "exists"
        return
    row.enabled = True
    # Stamped as an operator edit: `tests/test_cron_system_activities.py` pins the seeded row disabled and
    # skips rows whose `updated_by_id` differs from `created_by_id`. Leaving them equal would look like the
    # migration seeded it enabled.
    row.updated_by_id = _bootstrap_admin_id(session) or row.updated_by_id
    # Then ask the serving api to project it: DBOS schedules are projected by the api process, and this
    # one-shot session has no launched engine, so the row alone would stay PAUSED. The ask is enqueued in
    # this transaction, so it exists iff `app/seed.py::main` commits. Imported locally: it drags DBOS in.
    from app.durable.cron_projection import request_projection_in_transaction

    request_projection_in_transaction(session)
    created["pipeline_cron"] = "enabled"
