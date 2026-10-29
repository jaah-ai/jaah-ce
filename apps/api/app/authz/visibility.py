"""Row visibility as SQL: the `visible_*_clause` predicates, `OBJECT_SPECS` and `scoped_*_select`."""
from __future__ import annotations

import functools
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import Select, and_, false, literal, or_, select, true
from sqlalchemy.orm import Session, aliased

from app.entity_shape import optional_uuid
from app.models.agent_sessions import AgentSession
from app.models.credentials import Account, Credential, ProjectCredential
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.projects import Feature, Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
# The occupancy vocabulary is imported, never retyped as a literal (no import cycle back here).
from app import slot_state
from app.task_lifecycle import AWAITING_RESPONSE, DRAFT
from app.authz.catalog import (
    ACCOUNTS_VIEW_KEY,
    CREDENTIALS_DELETE_KEY,
    CREDENTIALS_VIEW_KEY,
    NODES_VIEW_KEY,
    PERMISSION_KEYS,
    PROJECTS_VIEW_KEY,
    permission_key,
)
from app.authz.principal import Principal, creates_org_rows


# ── The object-access clauses ─────────────────────────────────────────────────────────────────────
#
# Tasks have two clauses: a `tasks:view` grant reaches non-hidden tasks for reads, nothing for writes.
# One shared clause would widen every task mutation, since `can_access` serves both paths.


class ObjectAccess(Enum):
    """READ or WRITE — which of the two task clauses a single-object probe means.

    Not `(str, Enum)`: a `str` mixin would let a bare string silently select a branch."""

    READ = "read"
    WRITE = "write"


# ── The key-scoped SCOPE rule ──────────────────────────────────────────────────────────
#
# Every decision naming an access scope asks "does this principal hold key K on scope X", answered only
# by `scope_key_clause` (subject paths: `Principal.scope_permissions`). The scope type comes from
# `ObjectSpec.key_scope_type`.

#: The key `visible_task_clause`'s scope arm carries — the `tasks:view` widening, the
#: arm's own key. Checked at import: a misspelt literal would silently retire the arm.
TASK_SCOPE_ARM_KEY = permission_key("tasks", "view")
if TASK_SCOPE_ARM_KEY not in PERMISSION_KEYS:
    raise RuntimeError(f"TASK_SCOPE_ARM_KEY {TASK_SCOPE_ARM_KEY!r} is not in PERMISSION_KEYS")

#: The write-side keys for `editable_task_clause`'s scope arm: `_ACTION_OBJECT_ACCESS` maps both `edit`
#: and `delete` onto WRITE, so pinning to `edit` would silently refuse a `tasks:delete` holder. `create`
#: is absent: it names no existing row. Checked at import.
TASK_WRITE_SCOPE_ARM_KEYS = (permission_key("tasks", "edit"), permission_key("tasks", "delete"))
for _k in TASK_WRITE_SCOPE_ARM_KEYS:
    if _k not in PERMISSION_KEYS:
        raise RuntimeError(f"TASK_WRITE_SCOPE_ARM_KEYS member {_k!r} is not in PERMISSION_KEYS")
#: The set this pair must mirror, asserted exactly under `_ACTION_OBJECT_ACCESS`'s definition.
_EXPECTED_TASK_WRITE_ARM_ACTIONS = {"edit", "delete"}


def scope_key_clause(principal: Principal | None, scope_type: str, key: str, column):
    """A boolean clause selecting the rows whose `column` names a `scope_type` scope `principal` holds
    `key` on (`column` is e.g. `Project.id` or `Task.project_id`).

    `true()` for the agent principal, a superadmin and an org-wide holder of `key`; `false()` when no scope
    qualifies (never `IN ()`). An `IN` over the set resolved once in `app/access.py::expand`, not a
    correlated EXISTS, so the rule has one home and the clause has no cross-join hazard."""
    # A system project (`Project.system_kind`, `app/node_targets.py`) and everything in it is shown to org admins
    # and operators only, whichever arm admits the row: `Principal.system_project_ids`, resolved
    # once like the scopes, is empty for them and for every org without one.
    hidden = principal.system_project_ids if principal is not None and scope_type == "project" else frozenset()
    if principal is None or principal.has_org(key):
        # The org arm: an org-wide key reaches every scope, bounded to the principal's org by the seam.
        return column.not_in(sorted(hidden)) if hidden else true()
    allowed = principal.scopes_with(scope_type, key) - hidden
    if not allowed:
        return false()
    # Sorted, so the compiled statement is stable for statement caching.
    return column.in_(sorted(allowed))


def has_scope_key(
    principal: Principal | None, scope_type: str, key: str, scope_id: int | UUID | None
) -> bool:
    """`scope_key_clause` for one already-known scope id — the same question, no SQL.

    Exists for the None arm: the agent principal is allow-all, and a bare method call on it would raise
    inside an authorization decision. A None `scope_id` answers False, like `NULL IN (…)`. A system project
    answers False for a principal that is not an org admin, as `scope_key_clause` excludes it."""
    if principal is None:
        return True
    if scope_type == "project" and scope_id in principal.system_project_ids:
        return False
    if principal.is_superadmin:
        return True
    return scope_id is not None and principal.has_on_scope(scope_type, scope_id, key)


def has_project_key(principal: Principal | None, key: str, project_id: int | None) -> bool:
    """`has_scope_key` pinned to `"project"`, for call sites that genuinely mean a project. Never widen
    it; "the scope this row lives in" asks `has_scope_key` with `ObjectSpec.key_scope_type`.

    Current callers (all genuinely mean a project): `routes/project_env.py::_authorize_tier`,
    `routes/agent_project_settings.py::_require_the_launchers_project_key`, and in `routes/tasks/` the
    advisor/chat `worktrees:edit` gates and `tasks:edit` on a cross-project move's destination, and
    `governance/guard_escalation.py::answer_for` (`compliance:edit` to grant a waiver from a guard question).
    `app/slack_inbound.py` uses `key_scope_satisfied` instead."""
    return has_scope_key(principal, "project", key, project_id)


def unhidden_task_clause():
    """`hidden` rows stay out of sight EXCEPT while they carry an open question (`awaiting_response`): a
    question needs somebody to see and answer it wherever it was filed. The one home for that
    rule — the hidden gate below and the default board (`routes/tasks/listing.py::board_row_clauses`) both read it.
    A runner-break row (the leak the hidden gate exists for) is born `system_failed`, never asking."""
    return or_(Task.hidden.is_(False), Task.state == AWAITING_RESPONSE)


def visible_task_clause(principal: Principal | None):
    """A boolean clause selecting the tasks `principal` may see. Correlates to the enclosing `select(Task)`.

    Use only via `scoped_task_select` or `can_access`: a bare `select(clause)` is safe only while every
    conjunct is a `tasks` predicate (see `visible_project_clause`; `test_authz_sql.py`).

    A task is not grantable; access derives from its project plus keys held there under the same subject
    path. Three conjuncts, all about the row:
    * unsaved-draft gate — seen only by its author and a superadmin (`models/tasks.py::Task.unsaved`);
    * scope arm — `tasks:view` on the task's project; every holder sees every task there;
    * hidden gate — a `hidden` row only by its creator, a superadmin and an operator acting as the org (else
      `?include_hidden=true` would leak runner-break bodies), unless it has an open question (`_hidden_gate`)."""
    if principal is None or principal.is_superadmin:
        return true()
    return and_(
        # The gates sit outside any `or_`: they are about the row, so a new arm inherits them. Both
        # columns are NOT NULL, so `is_(False)` is honest.
        or_(Task.unsaved.is_(False), Task.created_by_id == principal.user_id),
        # A bare `IN` over the principal's resolved triples; its `tasks` predicate anchors the outer FROM.
        scope_key_clause(principal, "project", TASK_SCOPE_ARM_KEY, Task.project_id),
        _hidden_gate(principal),
    )


def _hidden_gate(principal: Principal):
    """The hidden gate both task clauses share: a `hidden` row only by its creator, unless it is asking
    (`unhidden_task_clause`) or a merge-lane run (`_lane_run_clause`). An operator acting as the org
    (`Principal.acting`) passes it, as the operator itself does (superadmin): otherwise a hidden task's page renders
    for the operator but every write the console sends as that org 404s, and its slot's hold cannot be released from
    either seat."""
    if principal.acting:
        return true()
    return or_(unhidden_task_clause(), Task.created_by_id == principal.user_id, _lane_run_clause())


def _lane_run_clause():
    """A merge-lane run (`app/merge_lane.py`): hidden only to keep it off the board, so READABLE by every
    `tasks:view` holder on its project — the Merge lane tab links each run to its task, whoever launched it
    — and WRITABLE by every task-write key holder there (`editable_task_clause`), so an org admin
    can Stop or Start a run whoever launched it. A lane run is a script's: it carries no
    runner-break body."""
    # Local: keeps the lane's import chain out of this module's.
    from app import merge_lane_state  # noqa: PLC0415

    return Task.persona == merge_lane_state.LANE_PERSONA


def editable_task_clause(principal: Principal | None):
    """A boolean clause selecting the tasks `principal` may mutate. Correlates to the enclosing
    `select(Task)`.

    Use only via `can_access`/`require_object_access` with `mode=ObjectAccess.WRITE`, or embedded in
    `worktree_editable_clause`. The read rule's three conjuncts with the key swapped for a task-write key
    (`TASK_WRITE_SCOPE_ARM_KEYS`: `tasks:edit` or `tasks:delete`) on the project. A separate function, not
    a flag, so the write rule can never acquire the read key by omission.

    Both write keys because `delete_task` declares `tasks:delete`. The key lives in the clause because bare
    embeddings (the console, `pool_slots.set_slot_title`) have no declaring route. No named-role arm (the
    scope arm subsumes it). Hidden/unsaved gates as
    for reads: a row you may not read is not one you may write."""
    if principal is None or principal.is_superadmin:
        return true()
    return and_(
        or_(Task.unsaved.is_(False), Task.created_by_id == principal.user_id),
        # Per key via `scope_key_clause`: short-circuits superadmin and never compiles `IN ()`.
        or_(*(
            scope_key_clause(principal, "project", key, Task.project_id)
            for key in TASK_WRITE_SCOPE_ARM_KEYS
        )),
        _hidden_gate(principal),
    )


# `authored_draft_task_clause`'s child alias, built once: a fresh `aliased(Task)` proxies every `Task` column, a few
# ms on each board read. Shared safely: the EXISTS correlates `Task` explicitly, never `child`.
@functools.cache
def _draft_child() -> Any:
    return aliased(Task)


def authored_draft_task_clause(principal: Principal | None):
    """A boolean clause selecting the top-level `draft` tasks `principal` authored — for when
    `AUTHOR_DRAFT_KEY` stands in for a task-write route's declared key. Correlates to the enclosing
    `select(Task)` via a correlated EXISTS (full cross-join hazard); use only via `authored_draft`.

    Not a mode of task access (absent from `OBJECT_SPECS`); `author_draft_admits` is the one predicate the
    guard and `add_task_dependency` consult. Conjuncts: authored by the caller; `state == DRAFT`; not
    `hidden`; `parent_task_id IS NULL` (refuses an epic's fan-out children, which carry the epic creator's
    `created_by_id`); and no non-draft child (delete, move and conversion reach the subtree). No
    `unsaved` conjunct: an auto-saved row is exactly the author's draft."""
    if principal is None or principal.is_superadmin:
        # Unreachable from `_enforce`; gives in-process callers the uniform allow-all answer.
        return true()
    child = _draft_child()
    return and_(
        Task.created_by_id == principal.user_id,
        Task.state == DRAFT,
        Task.hidden.is_(False),
        Task.parent_task_id.is_(None),
        ~select(literal(1))
        .select_from(child)
        .where(child.parent_task_id == Task.id, child.state != DRAFT)
        .correlate(Task)
        .exists(),
    )


def visible_project_clause(principal: Principal | None):
    """A boolean clause selecting the projects `principal` may see: `projects:view` on it — org-wide, or
    through a binding on the project (`app/access.py::expand`). A plain
    predicate on `Project.id`; use via `scoped_object_select`/`can_access`."""
    return scope_key_clause(principal, "project", PROJECTS_VIEW_KEY, Project.id)


def visible_feature_clause(principal: Principal | None):
    """A boolean clause selecting the features `principal` may see: `tasks:view` on the feature's
    project — the key that shows that project's tasks, so a feature is seen exactly where its tasks
    are (D9). Features carry no permission model of their own; a write is the same scope with the route's
    key (`FEATURE_SPEC.key_scoped_modes`)."""
    return scope_key_clause(principal, "project", TASK_SCOPE_ARM_KEY, Feature.project_id)


# ── The node, account and credential decisions (bindings; D5/D6/D7/D13, E1/E2) ────────────────────


def _reached_project_ids(principal: Principal):
    """The projects `principal` reaches through a SCOPED binding — the non-correlated set a node or
    credential arm hangs off. The org arm is decided by the caller (`has_org`)."""
    return sorted(principal.scopes_with("project", PROJECTS_VIEW_KEY))


def node_reach_clause(principal: Principal | None, column):
    """May `principal` see the node `column` names? True for the agent principal, a superadmin and an
    org-wide `nodes:view` holder; otherwise the node hosts a worktree of a project the principal reaches
    (D13: there is no node binding).

    A non-correlated `IN` subquery over `worktrees`, so any `column` works — the
    node's own id, `Worktree.node_id`, `Account.node_id`."""
    if principal is None or principal.has_org(NODES_VIEW_KEY):
        return true()
    if principal.has_org(PROJECTS_VIEW_KEY):
        # A project role bound org-wide reaches every project: every node hosting one (the seam bounds
        # the subquery to the principal's org).
        return column.in_(select(Worktree.node_id))
    projects = _reached_project_ids(principal)
    if not projects:
        return false()
    return column.in_(select(Worktree.node_id).where(Worktree.project_id.in_(projects)))


def visible_node_clause(principal: Principal | None):
    """A boolean clause selecting the nodes `principal` may see (`node_reach_clause` on `Node.id`).

    Presence-only hosts (no `nodes` row, no uuid) are operator-only (`sees_all_nodes`)."""
    return node_reach_clause(principal, Node.id)


def visible_node_ref_clause(principal: Principal | None, column):
    """May `principal` see a row whose nullable node reference is `column`? `node_reach_clause` plus a
    NULL arm: a row naming no node (`SET NULL` on `CommandAudit.node_id`, `RunTelemetry.node_id`)
    discloses none."""
    return or_(column.is_(None), node_reach_clause(principal, column))


def visible_session_clause(principal: Principal | None):
    """May `principal` see this agentic session (`AgentSession`)? Its node reach (`visible_node_ref_clause`), minus
    the sessions of a system project hidden from it (`Principal.system_project_ids`): a node task's run is that
    project's. Correlates to the enclosing `select(AgentSession)`."""
    reach = visible_node_ref_clause(principal, AgentSession.node_id)
    hidden = principal.system_project_ids if principal is not None else frozenset()
    if not hidden:
        return reach
    return and_(reach, or_(AgentSession.project_id.is_(None), AgentSession.project_id.not_in(sorted(hidden))))


def sees_all_nodes(principal: Principal | None) -> bool:
    """Whether `principal` is under no node restriction at all — the agent principal (None) or the
    deployment's operator.

    A Python bool for the one question a clause cannot answer: presence-only entries in `GET /api/nodes`
    have no row, so under confinement they are operator-only. `is_operator`, not `is_superadmin`: a tenant
    superadmin passing here let `routes/commands.py` dispatch to another tenant's node by raw bus key."""
    return principal is None or principal.is_operator


def visible_account_clause(principal: Principal | None):
    """A boolean clause selecting the credential accounts `principal` may DISPATCH on (D5/D10, E2): its
    own personal account, an account-scope binding, or — with org-wide `accounts:view` — any ORG account
    (`owner_id IS NULL`). Someone else's personal account is never in it, an Org Owner/Admin or a
    superadmin included: personal stays personal. Correlates to the enclosing `select(Account)`; a roster
    read is `readable_account_clause`."""
    if principal is None:
        return true()
    arms = [Account.owner_id == principal.user_id]
    if principal.has_org(ACCOUNTS_VIEW_KEY):
        arms.append(Account.owner_id.is_(None))
    bound = sorted(principal.scopes_with("account", ACCOUNTS_VIEW_KEY), key=str)
    if bound:
        # A binding shares an ORG account; it never reaches a colleague's personal one.
        arms.append(and_(Account.id.in_(bound), Account.owner_id.is_(None)))
    return or_(*arms)


def readable_account_clause(principal: Principal | None):
    """What an account read means — behind `readable_account_select` and every
    `can_access(..., "account", ..., mode=READ)` probe, so the roster and the SSE relay agree.

    The dispatch clause and, while node reads are scoped, the node the account is pinned to (`IS NULL` for
    unpinned). The early return is the coupling to `NODE_SPEC.unscoped_read_key`;
    `ObjectSpec.roster_read_clause` explains both."""
    dispatchable = visible_account_clause(principal)
    if NODE_SPEC.unscoped_read_key is not None:
        return dispatchable
    return dispatchable & (Account.node_id.is_(None) | node_reach_clause(principal, Account.node_id))


def _pool_of(project_ids):
    """The credential is in one of `project_ids`' pools: attached (`project_credentials`) or HOMED there
    (`Credential.home_project_id` — a home project counts as attached, `Credential`'s NAMING)."""
    return or_(
        Credential.home_project_id.in_(project_ids),
        Credential.id.in_(
            select(ProjectCredential.credential_id).where(ProjectCredential.project_id.in_(project_ids))
        ),
    )


def visible_credential_clause(principal: Principal | None):
    """A boolean clause selecting the credentials whose METADATA `principal` may read (never the secret;
    use is `usable_credential_clause`): its own, org-wide `credentials:view`, or one attached to or homed in
    a project it holds `credentials:view` on (`_pool_of`). Correlates to the enclosing `select(Credential)`.

    `None` is allow-all (the agent principal); never widen a "chain names nobody" answer into it —
    `routes/agent/task_secrets.py::_task_grant_principal`'s `None` means deny. The git-token routes are a separate,
    role-resolved surface (`app/git_access.py`)."""
    if principal is None:
        return true()
    if principal.has_org(CREDENTIALS_VIEW_KEY):
        return _system_hidden_from(principal)
    arms = [Credential.owner_id == principal.user_id]
    pooled = sorted(principal.scopes_with("project", CREDENTIALS_VIEW_KEY))
    if pooled:
        arms.append(_pool_of(pooled))
    return and_(_system_hidden_from(principal), or_(*arms))


def sees_system_credentials(principal: Principal | None) -> bool:
    """May `principal` see and manage a SYSTEM credential (`Credential.system`, 0325) — one the control
    plane itself acts with? An Org Owner/Admin or an operator, and the agent principal (`None`, the control
    plane's own reads). The same bar as `creates_org_rows`. Seeing is all it confers: a system row is
    never USABLE by a task (`usable_credential_clause`), whoever launched it."""
    return creates_org_rows(principal)


def _system_hidden_from(principal: Principal | None):
    """`true()` for a caller who `sees_system_credentials`; otherwise only the non-system rows."""
    return true() if sees_system_credentials(principal) else Credential.system.is_(False)


def owned_credential_clause(principal: Principal | None):
    """A boolean clause selecting the credentials that are `principal`'s own (`owner_id`, G3) — the
    strictly smaller set a keyless caller gets, and the whole enforcement behind
    `Declaration.own_credential`.

    A superadmin is answered like anyone else: `routes/credentials/roster.py::roster_scope` calls this for
    `?mine=true`, where allow-all would list every credential as "mine"."""
    if principal is None:
        # The agent principal: no user id to compare ownership against.
        return true()
    return Credential.owner_id == principal.user_id


def usable_credential_clause(principal: Principal, project_id: int | None):
    """A boolean clause selecting the credentials a task on `project_id` launched by `principal` may USE
    — secrets included (E1, AC-3.2). Three arms:
    * attached — in `project_id`'s pool (attached or homed there, `_pool_of`) and unowned, with
      `credentials:view` on that project or org-wide;
    * personal — `owner_id` is the launcher. A personal credential is NEVER usable by anyone else, an
      Org Owner/Admin included;
    * org — unowned (`owner_id IS NULL`) and the launcher holds an org role (D7).

    NO SUPERADMIN BYPASS and no `None` arm: nothing resolving without a person may inherit allow-all, so
    this takes a `Principal`, and `is_superadmin` is never read. Account API keys are excluded by the
    selects built from it (`task_roster_select`, `app/project_env.py`)."""
    arms = [Credential.owner_id == principal.user_id]
    if project_id is not None:
        key = CREDENTIALS_VIEW_KEY
        if key in principal.org_keys or ("project", project_id, key) in principal.scope_permissions:
            # A pooled row is usable only while it is the ORG's: a personal credential in a pool is still
            # its owner's alone (the owner arm above).
            arms.append(and_(_pool_of([project_id]), Credential.owner_id.is_(None)))
    if principal.org_role is not None:
        arms.append(Credential.owner_id.is_(None))
    # The launcher's own org, restated: the seam bounds it too, but a roster read on the operator plane
    # (a node-less agent caller) must not reach another tenant's org credential.
    # A SYSTEM row is the control plane's own and never a task's (0325), whoever launched it.
    return and_(_own_org(principal), Credential.system.is_(False), or_(*arms))


def _own_org(principal: Principal):
    """`Credential.org_id` is the principal's own org — `false()` for an org-less principal, never the
    `IS NULL` a bare comparison would render."""
    return Credential.org_id == principal.org_id if principal.org_id is not None else false()


def _not_an_account_key():
    """Not an account's own API key — excluded by being pointed at by an `accounts` row (as
    `routes/accounts.py::_drop_owned_api_key_credential` counts), not by type or name."""
    return Credential.id.not_in(
        select(Account.credential_id).where(Account.credential_id.is_not(None))
    )


# The operator knobs whose credential the control plane itself acts with, never a task. A knob retired from this
# tuple leaves its rows unfenced, so its retirement makes them SYSTEM rows first (the deleted PR review lane's two:
# revision 202610251237).
_CONTROL_PLANE_SECRET_KNOBS: tuple[str, ...] = (
    # The platform Bedrock key (`app/bedrock/account.py`) is refused unless System already; named here too so
    # a mis-flagged row still never reaches a task.
    "platform_bedrock_credential_id",
    # The evidence collectors' GitHub token (`app/compliance_evidence/github.py`) reads repository
    # administration: the control plane's to act with, never a task's.
    "compliance_github_credential_id",
)


def _not_a_control_plane_secret():
    """Not a credential the control plane itself acts with: any SYSTEM row (0325), any `github_app` or
    `github_install_app` row (the GitHub OAuth App's or GitHub App's client secret, which Connect GitHub and
    GitHub sign-in spend and is never a task's; an older row may still hold a GitHub App's
    private key) and the rows `_CONTROL_PLANE_SECRET_KNOBS` name."""
    from app.operator_settings import reads as settings_reads

    ids = [parsed for key in _CONTROL_PLANE_SECRET_KNOBS
           if (parsed := optional_uuid(settings_reads.get_str(key).strip())) is not None]
    clause = and_(Credential.type.not_in(("github_app", "github_install_app")), Credential.system.is_(False))
    return and_(clause, Credential.id.not_in(ids)) if ids else clause


def task_roster_select(principal: Principal, project_id: int | None) -> Select:
    """The credentials a task on `project_id` may use under its launcher `principal` — the one home for
    `routes/agent/task_secrets.py::_granted_credentials` (the MCP roster and by-path get) and project-env resolution
    (`app/project_env.py`, `routes/project_env.py`). `usable_credential_clause` minus account API keys and
    the control plane's own secrets."""
    return select(Credential).where(
        usable_credential_clause(principal, project_id), _not_an_account_key(), _not_a_control_plane_secret()
    )


# ── THE OBJECT-TYPE TABLE — one row per access-controlled type, and the single home for the set ────


@dataclass(frozen=True)
class ObjectSpec:
    """One access-controlled object type, declared once. Every consumer (clause, id parse, scoped select,
    SSE filter) derives from `OBJECT_SPECS`, so a new type is one row; per-consumer arms fail open
    silently."""

    name: str
    """`project` | `task` | `node` | `account` | `credential` | `feature` — the `object_type` a route
    declares."""
    entity: type
    """The mapped class. `entity.id` is what a scoped select projects and what `can_access` matches."""
    id_type: type
    """`int` or `UUID` — the DOMAIN of this type's id. The guard parses a raw path segment with it
    (`spec.id_type(raw)`), failing closed on a mismatch."""
    read_clause: Callable
    """The clause a READ probe means."""
    roster_read_clause: Callable | None = None
    """The clause a READ means when THE ROW ITSELF DISCLOSES ANOTHER OBJECT'S IDENTITY, so the roster read
    is narrower than `read_clause`. `None` means `read_clause` serves both; `account` is the exception
    (`readable_account_clause` — an `AccountOut` names the node the account is pinned to). A second clause
    rather than a replacement: `read_clause` is what dispatch means (`visible_account_clause`), and
    `can_access` answers on this one so the SSE relay never announces an account whose by-id route 404s.
    The extra conjunct is coupled to `NODE_SPEC.unscoped_read_key`
    (`test_the_account_node_conjunct_is_coupled_to_the_node_read`)."""
    unscoped_read_key: str | None = None
    """SET ONLY WHILE THIS TYPE'S COLLECTION READ IS NOT SCOPED — the feature key that read requires
    instead, so the SSE filter is never narrower than the read it mirrors. `None` everywhere today."""
    write_clause: Callable | None = None
    """The clause a WRITE probe means, when it DIFFERS from the read clause — `task` (reading is
    `tasks:view` on its project, writing `tasks:edit`/`tasks:delete` there)."""
    key_scope_column: Any | None = None
    """WHICH SCOPE MUST THE EXERCISED KEY BE HELD ON, for a row of this type? A column on
    `entity`, or `None` for a type whose keys are not scope-scoped. `key_scope_satisfied` asks
    `has_scope_key` about it: `project` names its own id, `task` and `feature` their project."""
    key_scope_type: str = "project"
    """WHICH KIND OF SCOPE `key_scope_column` points at — an `OBJECT_SPECS` name, validated at import."""
    key_scoped_modes: tuple["ObjectAccess", ...] = ()
    """WHICH PROBE MODES consult the key-scope rule. `task` names WRITE only: `visible_task_clause` is
    already key-scoped on `tasks:view`, and on WRITE the conjunct pins the ROUTE'S key to the task's
    project."""

    def key_scoped_in(self, mode: "ObjectAccess") -> bool:
        """Does a `mode` probe of this type consult the key-scope rule? The one reader of
        `key_scoped_modes`."""
        return mode in self.key_scoped_modes


PROJECT_SPEC = ObjectSpec(
    name="project", entity=Project, id_type=int,
    read_clause=visible_project_clause,
    key_scope_column=Project.id, key_scope_type="project",
    key_scoped_modes=(ObjectAccess.READ, ObjectAccess.WRITE),
)
TASK_SPEC = ObjectSpec(
    name="task", entity=Task, id_type=int,
    read_clause=visible_task_clause, write_clause=editable_task_clause,
    # Key-scoped on writes only, against the task's project.
    key_scope_column=Task.project_id, key_scope_type="project",
    key_scoped_modes=(ObjectAccess.WRITE,),
)
NODE_SPEC = ObjectSpec(
    name="node", entity=Node, id_type=UUID,
    read_clause=visible_node_clause,
    # No `unscoped_read_key`: `routes/nodes/reads.py::list_nodes` is scoped, so the SSE relay may filter.
)
ACCOUNT_SPEC = ObjectSpec(
    name="account", entity=Account, id_type=UUID,
    read_clause=visible_account_clause,
    roster_read_clause=readable_account_clause,
)
CREDENTIAL_SPEC = ObjectSpec(
    name="credential", entity=Credential, id_type=UUID,
    read_clause=visible_credential_clause,
)
FEATURE_SPEC = ObjectSpec(
    name="feature", entity=Feature, id_type=UUID,
    read_clause=visible_feature_clause,
    # Not grantable: a feature's access is its project's task keys, in both modes (D9) — the READ
    # route key is `tasks:view`, a write's `tasks:edit`, each held on the feature's project.
    key_scope_column=Feature.project_id, key_scope_type="project",
    key_scoped_modes=(ObjectAccess.READ, ObjectAccess.WRITE),
)

#: Every access-controlled object type. A worktree is not a member: its access is derived.
OBJECT_SPECS: tuple[ObjectSpec, ...] = (
    PROJECT_SPEC, TASK_SPEC, NODE_SPEC, ACCOUNT_SPEC, CREDENTIAL_SPEC, FEATURE_SPEC,
)

#: The type names, derived.
OBJECT_TYPES = tuple(spec.name for spec in OBJECT_SPECS)

#: Name -> spec, for callers holding a wire string.
OBJECT_SPEC_BY_NAME: dict[str, ObjectSpec] = {spec.name: spec for spec in OBJECT_SPECS}

if len(OBJECT_SPEC_BY_NAME) != len(OBJECT_SPECS):
    raise RuntimeError(f"OBJECT_SPECS has duplicate names: {[s.name for s in OBJECT_SPECS]}")

# Every `unscoped_read_key` is a real catalog key (a misspelt one would silently deny every reader).
_unknown_read_keys = sorted(
    {spec.unscoped_read_key for spec in OBJECT_SPECS if spec.unscoped_read_key} - PERMISSION_KEYS
)
if _unknown_read_keys:
    raise RuntimeError(
        f"OBJECT_SPECS names unscoped_read_key(s) {_unknown_read_keys} outside PERMISSION_KEYS"
    )

# Every `key_scope_type` names a real object type (a misspelt one matches no triple: silent deny).
_unknown_scope_types = sorted(
    {spec.key_scope_type for spec in OBJECT_SPECS} - set(OBJECT_SPEC_BY_NAME)
)
if _unknown_scope_types:
    raise RuntimeError(
        f"OBJECT_SPECS names key_scope_type(s) {_unknown_scope_types} outside OBJECT_TYPES"
    )
# Every `key_scope_column` reaches its declared type's table — its own key, or through a foreign key
# (a valid-but-wrong pairing would silently deny the scope arm). A membership test, since a column may
# reach its scope table only through a composite constraint.
_mispaired_scope_columns = []
for _spec in OBJECT_SPECS:
    if _spec.key_scope_column is None:
        continue
    _column = _spec.key_scope_column.expression
    _scope_table = OBJECT_SPEC_BY_NAME[_spec.key_scope_type].entity.__table__
    _reaches = {fk.column.table for fk in _column.foreign_keys} or {_column.table}
    if _scope_table not in _reaches:
        _mispaired_scope_columns.append(
            f"{_spec.name}: {_column} does not reach {_scope_table.name} "
            f"(key_scope_type={_spec.key_scope_type!r})"
        )
if _mispaired_scope_columns:
    raise RuntimeError(
        f"OBJECT_SPECS pairs a key_scope_column with the wrong key_scope_type: "
        f"{_mispaired_scope_columns}"
    )

# No key-scope column without modes to consult it (a rule that reads live and is never asked).
_inert_key_scope = sorted(
    spec.name for spec in OBJECT_SPECS if not spec.key_scoped_modes and spec.key_scope_column is not None
)
if _inert_key_scope:
    raise RuntimeError(
        f"OBJECT_SPECS declares key-scope columns on {_inert_key_scope} with no key_scoped_modes to "
        "consult them: name the modes, or drop the columns"
    )
# Every mode is an `ObjectAccess` member; a string would silently retire the conjunct.
_bad_modes = sorted(
    f"{spec.name}:{mode!r}"
    for spec in OBJECT_SPECS
    for mode in spec.key_scoped_modes
    if not isinstance(mode, ObjectAccess)
)
if _bad_modes:
    raise RuntimeError(f"OBJECT_SPECS names non-ObjectAccess key_scoped_modes: {_bad_modes}")


@dataclass(frozen=True)
class AllowedObjects:
    """What one subject may reach, of one object type — an explicit value, never `None`-means-all.

    `frozenset | None` puts unrestricted and deny-everything one truthiness slip apart (fail-open). Here
    `unrestricted=False, ids=frozenset()` is deny-by-default (D2) and a different value from
    `unrestricted=True`."""

    unrestricted: bool
    ids: frozenset

    def contains(self, object_id) -> bool:
        """May this subject reach `object_id`? The one reader of the pair."""
        return self.unrestricted or object_id in self.ids


#: The "no restriction at all" answer.
UNRESTRICTED_OBJECTS = AllowedObjects(unrestricted=True, ids=frozenset())


# ── The worktree decision — DERIVED access, never a third object type ──────────────────
#
# No `OBJECT_TYPES` member and no binding scope: a stored slot grant would be a second home for the
# task's own access decision. Derived at query time from the slot's task and hold ownership.

# The two pool-administration keys that widen worktree visibility (and nothing else — see
# `worktree_editable_clause`). Checked at import.
WIDE_WORKTREE_KEYS = ("worktrees:create", "worktrees:delete")
if set(WIDE_WORKTREE_KEYS) - PERMISSION_KEYS:
    raise ValueError(
        f"unknown permission keys {sorted(set(WIDE_WORKTREE_KEYS) - PERMISSION_KEYS)}; "
        "expected members of PERMISSION_KEYS"
    )

def sees_all_worktrees(principal: Principal | None) -> bool:
    """Whether `principal` sees every slot in the pool, whatever occupies it.

    A bool, not a clause: a property of the principal alone. True for the agent principal, a superadmin, or
    an ORG-WIDE holder of either `WIDE_WORKTREE_KEYS` key. Visibility only: `worktree_editable_clause` never consults
    it (`view` must not imply `edit`)."""
    if principal is None or principal.is_superadmin:
        return True
    # `has_org`, never `has`: a project Maintainer's `worktrees:create` reaches that project's slots
    # (`_worktree_pool_rule`), never the org's whole pool.
    return any(principal.has_org(key) for key in WIDE_WORKTREE_KEYS)


def worktree_editable_clause(principal: Principal | None):
    """A boolean clause selecting the worktrees `principal` may drive — send input to, open a
    read-write console on, retitle. Correlates to the enclosing `select(Worktree)`.

    Use only inside `select(Worktree)` or a `select_from(Worktree)`; otherwise every EXISTS goes
    true whenever the subject holds any hold or grant anywhere.

    Two derived arms: my own `user` hold (`_worktree_owner_arm`) and the task's write decision
    (`_console_task_clause`) with the liveness hold (`_worktree_task_arm`). Not widened by
    `WIDE_WORKTREE_KEYS` and not node-scoped. Editable ⊆ visible is not structural: a caller probing
    this clause must also conjoin the read clause (`console._console_slot_editable`,
    `pool_slots.set_slot_title`)."""
    if principal is None or principal.is_superadmin:
        return true()
    # The write task clause, never the read one: driving a console is the strongest write here, and a
    # read grant must never become an edit right. The slot stays project-scoped
    # (delivery, not access).
    return or_(_worktree_owner_arm(principal), _worktree_task_arm(principal, _console_task_clause))


def _console_task_clause(principal: Principal | None):
    """`editable_task_clause` pinned to `tasks:edit` — for a slot's keyboard, retitle and release, where
    no route declares a key for the guard to re-pin.

    Without it a `tasks:delete`-only holder could drive every runner in the project; a PTY relay is the
    interactive form of start/stop/answer, which are `tasks:edit`. Conjoined, so the hidden/unsaved gates
    ride along."""
    if principal is None or principal.is_superadmin:
        return true()
    return and_(
        editable_task_clause(principal),
        scope_key_clause(principal, "project", permission_key("tasks", "edit"), Task.project_id),
    )


def _worktree_owner_arm(principal: Principal):
    """The user-hold arm of the worktree clauses: true when `principal` currently holds this slot —
    `state = 'user'` and `owner_user_id` is theirs.

    Ownership of the hold is the whole claim; no grant or key reaches it. `ck_worktrees_state_ownership`
    makes `owner_user_id` NULL for `free`/`task` rows. An EXISTS correlated to `Worktree`, not a bare
    predicate, so it is as unsafe-to-call-bare as the task arm
    (`test_the_bare_worktree_clauses_compile_to_a_cross_join`)."""
    return (
        select(literal(1))
        .where(Worktree.state == slot_state.USER, Worktree.owner_user_id == principal.user_id)
        .correlate(Worktree)
        .exists()
    )


def _worktree_task_arm(principal: Principal | None, task_clause):
    """The task arm of both worktree clauses: the slot's task satisfies `task_clause` and the slot is
    actually held. Correlates to `Worktree`.

    One builder for `_console_task_clause` (keyboard) and `visible_task_clause` (eyes), so the liveness half
    has one copy. The hold is the liveness proof: `worktrees.task_id` is stale after a reap. A `user`-held
    row never matches (its `task_id` is NULL)."""
    return (
        select(literal(1))
        # The plain `Task` entity, never `aliased(Task)`: the task clauses correlate to `Task`, so an
        # alias would reintroduce the cross join one level down.
        .select_from(Task)
        # `worktrees.task_id` has no FK on purpose; a NULL or dangling id simply matches no row.
        .where(
            Task.id == Worktree.task_id,
            task_clause(principal),
            Worktree.state != slot_state.FREE,
        )
        .correlate(Worktree)
        .exists()
    )


def _worktree_pool_rule(principal: Principal | None):
    """The pool-derived half of `visible_worktree_clause`, with no node scoping; factored out
    only so the node conjunct can wrap its `true()` arm too. Never call directly.

    The editable set, plus the task-read widening (`visible_task_clause`), plus `WIDE_WORKTREE_KEYS` —
    org-wide (every slot), or on a project (that project's slots, free ones included). A free slot is
    otherwise invisible to a scoped viewer. Built as `or_(editable, …)` so editable ⊆ visible."""
    hidden = principal.system_project_ids if principal is not None else frozenset()
    if sees_all_worktrees(principal):
        # A system project's slots stay hidden from a non-admin even here (`Principal.system_project_ids`).
        return Worktree.project_id.not_in(sorted(hidden)) if hidden else true()
    arms = [worktree_editable_clause(principal), _worktree_task_arm(principal, visible_task_clause)]
    wide_projects = sorted(
        {pid for key in WIDE_WORKTREE_KEYS for pid in principal.scopes_with("project", key)} - hidden
    )
    if wide_projects:
        arms.append(Worktree.project_id.in_(wide_projects))
    return or_(*arms)


def visible_worktree_clause(principal: Principal | None):
    """A boolean clause selecting the worktrees `principal` may see. Correlates to the enclosing
    `select(Worktree)` (hazard as `worktree_editable_clause`); use only inside `select(Worktree)` or an
    enclosing `select_from(Worktree)`. `may_edit` is answered by selecting `worktree_editable_clause` as a
    column of that select, never per row or client-side.

    `_worktree_pool_rule` and the node reach (`node_reach_clause`; no NULL arm: `node_id` is NOT NULL).
    The conjunct sits outside the pool rule, so it can only narrow — and it wraps the `true()` arm too."""
    return and_(_worktree_pool_rule(principal), node_reach_clause(principal, Worktree.node_id))


def visible_task_vm_clause(principal: Principal | None):
    """The task MicroVMs whose console `principal` may SEE — `visible_worktree_clause`'s VM spelling (a VM task
    has no slot): its task is visible. No `node_reach_clause`: that reads `worktrees`, which a MicroVM
    node never holds, so it would refuse every scoped principal; the VM is the task's own runner, and the task
    detail already names its node. Needs `Microvm` and `Task` (joined on `Microvm.task_id`) in the enclosing
    FROM; the clause correlates to `Task`, as in `_worktree_task_arm`."""
    return visible_task_clause(principal)


def editable_task_vm_clause(principal: Principal | None):
    """The task MicroVMs whose console `principal` may DRIVE: the task's own `tasks:edit` decision, exactly as a
    held slot's keyboard (`_console_task_clause`). Same FROM as `visible_task_vm_clause`, which a caller must
    conjoin (editable ⊆ visible is not structural)."""
    return _console_task_clause(principal)


def scoped_task_select(principal: Principal | None) -> Select:
    """Shape 1 — the collection filter. `select(Task)` narrowed to what `principal` may see; for the
    agent principal and a superadmin it is deliberately unfiltered.

    Every collection handler builds from this, never a bare `select(Task)` plus a filter. Always a read
    shape; a bulk-write route would need its own helper over `editable_task_clause`."""
    return select(Task).where(visible_task_clause(principal))


def scoped_project_select(principal: Principal | None, *, key: str) -> Select:
    """Shape 1 for projects. See `scoped_task_select`.

    `key` is required and keyword-only: each caller names the capability it exercises
    (`GET /api/projects` is `projects:view`, the reserve's lookup `worktrees:edit`). `scope_key_clause`
    alone."""
    return select(Project).where(scope_key_clause(principal, "project", key, Project.id))


def scoped_object_select(principal: Principal | None, spec: ObjectSpec) -> Select:
    """Shape 1, generic — `select(spec.entity)` narrowed to what `principal` may read.

    Always `read_clause`. Not what a read route means for `account` (use `readable_account_select`) or for
    key-scoped types (use `scoped_project_select`)."""
    return select(spec.entity).where(spec.read_clause(principal))


def scoped_node_select(principal: Principal | None) -> Select:
    """SHAPE 1 for nodes (`node_reach_clause`)."""
    return scoped_object_select(principal, NODE_SPEC)


def scoped_account_select(principal: Principal | None) -> Select:
    """Shape 1 for credential accounts — what `principal` may DISPATCH on (`visible_account_clause`), not
    what an account read means (see `readable_account_select`). `granted_account_slugs` is the consumer."""
    return scoped_object_select(principal, ACCOUNT_SPEC)


def scoped_credential_select(principal: Principal | None) -> Select:
    """SHAPE 1 for credentials — the metadata read (`visible_credential_clause`)."""
    return scoped_object_select(principal, CREDENTIAL_SPEC)


def scoped_own_credential_select(principal: Principal | None) -> Select:
    """`select(Credential)` narrowed to `principal`'s own rows (`owned_credential_clause`)."""
    return select(Credential).where(owned_credential_clause(principal))


def edits_any_credential(principal: Principal | None) -> bool:
    """May `principal` edit and delete EVERY credential in its bound org — personal and system rows included?
    An operator acting as the tenant (`Principal.acting`) or an operator on its own plane
    (`is_operator`): the operator acts on the org's behalf. The one rule `credential_write_clause` and
    `routes/credentials/guards.py::_may_edit` both restate. Editing never grants USE: a task's roster is
    `usable_credential_clause`, untouched. Tenant Org Owners/Admins are not widened."""
    return principal is not None and (principal.acting or principal.is_operator)


def credential_write_clause(principal: Principal | None, key: str):
    """The credentials a route declaring `key` may touch: every row `visible_credential_clause` reads for
    `credentials:view`; for a write key, the caller's own rows, plus every ORG row (`owner_id IS NULL`)
    for an org-wide holder of that key (`has_org`, never `has`). Someone else's personal credential is
    not writable — re-pointing it would redirect its owner's runs — with two exceptions:
      · an operator, acting as the tenant or on its own plane (`edits_any_credential`), edits and deletes
        every credential in the bound org. Another org's row is reached by acting (the console sends
        `X-Jaah-Act-As` for that one request); unacting, `authz/enforce.py::OtherOrgDenial` says so;
      · DELETE only: `Principal.is_org_admin` (an Org Owner/Admin) deletes a personal credential owned by a
        member of its own org — the same test `validated_owner_user` asks to CREATE one for that member, so
        a mistaken create can be undone. Deleting redirects nothing."""
    if key == CREDENTIALS_VIEW_KEY:
        return visible_credential_clause(principal)
    if principal is None:
        return true()
    if edits_any_credential(principal):
        return _own_org(principal)
    arms = [owned_credential_clause(principal)]
    if principal.has_org(key):
        arms.append(and_(Credential.owner_id.is_(None), _system_hidden_from(principal)))
    if key == CREDENTIALS_DELETE_KEY and principal.is_org_admin:
        arms.append(_member_personal_clause(principal))
    return or_(*arms)


def _member_personal_clause(principal: Principal):
    """Personal credentials in `principal`'s own org whose owner is a member of that org. `admin_users` is
    never seam-narrowed, so the member test is this subquery. `validated_owner_user`'s test less its
    `disabled_at` arm: a departed member's credential is exactly what an admin needs to clean up."""
    members = select(AdminUser.id).where(
        AdminUser.org_id == principal.org_id, AdminUser.is_system.is_(False)
    )
    return and_(_own_org(principal), Credential.owner_id.in_(members))


def git_identity_select(principal: Principal, *, current_id: UUID | None = None) -> Select:
    """The credentials `principal` may push AS (Profile > Git) — a USE of the secret, so the use
    rule's shape: their own; an ORG credential (`owner_id IS NULL`) under an org role; and the org
    credential already selected (`current_id`), so a selection made before this rule keeps resolving.
    Never someone else's personal credential. One home for `git_access._readable_credential` and
    `routes/auth.py::git_credential_scope`; no superadmin arm."""
    arms = [Credential.owner_id == principal.user_id]
    if principal.org_role is not None:
        arms.append(Credential.owner_id.is_(None))
    elif current_id is not None:
        arms.append(and_(Credential.owner_id.is_(None), Credential.id == current_id))
    # A SYSTEM row is never pushed as: it is the control plane's own (0325).
    return select(Credential).where(_own_org(principal), Credential.system.is_(False), or_(*arms))


def scoped_credential_select_for(principal: Principal | None, key: str) -> Select:
    """The `select(Credential)` every credential route that scopes on its declared key builds from
    (`credential_write_clause`). Exception: `list_credentials?mine=true` asks about ownership and builds
    from `scoped_own_credential_select`."""
    return select(Credential).where(credential_write_clause(principal, key))


def readable_account_select(principal: Principal | None) -> Select:
    """`select(Account)` narrowed to what `principal` may see on the roster — the one home, so
    `GET /api/accounts`, its by-id twin and `GET /api/resources` (a field superset) agree.

    Built from `readable_account_clause`, which is also what `can_access` answers on, so the SSE relay
    cannot announce an account this drops."""
    return select(Account).where(readable_account_clause(principal))


def granted_account_slugs(session: Session, principal: Principal | None) -> AllowedObjects:
    """The dispatch clause projected to slugs — for a reader that discloses the account and nothing
    about its pinned node.

    `readable_account_slugs` is the roster answer with the node conjunct. Use this only where the payload
    does not disclose the pin: the task-activity `account_slug`, and a provisioning row (which names the
    flow's node, gated separately)."""
    return _account_slug_set(session, principal, scoped_account_select(principal))


def readable_account_slugs(session: Session, principal: Principal | None) -> AllowedObjects:
    """`readable_account_select` projected to slugs, for the reader that joins on the slug.

    Narrower than `granted_account_slugs` (carries the node conjunct). `/api/resources` joins the slug-keyed
    sensed fold (`app/account_fold.py`); `uq_accounts_slug` makes the slug a faithful id stand-in. A folded
    slug with no `accounts` row is not contained (deny by default; registering makes it grantable)."""
    return _account_slug_set(session, principal, readable_account_select(principal))


def _account_slug_set(
    session: Session, principal: Principal | None, statement: Select
) -> AllowedObjects:
    """The projection both slug helpers share; `statement` is their only difference.

    Returns `AllowedObjects`, not `frozenset | None`. The operator short-circuits before the query.
    `is_operator`, not `is_superadmin`: `UNRESTRICTED` tells `_readable_fold_records` to skip filtering a
    fleet-wide fold, so a tenant-org superadmin must run the projection under their bound org (otherwise
    `GET /api/resources` served other orgs' slugs, emails and credential-dir paths)."""
    if principal is None or principal.is_operator:
        return UNRESTRICTED_OBJECTS
    return AllowedObjects(
        unrestricted=False,
        # A blank slug is dropped: `accounts.slug` permits `""`, which would match every fold and
        # provisioning record with a blank slug and serve their config dirs and emails.
        ids=frozenset(
            slug
            for slug in session.scalars(statement.with_only_columns(Account.slug)).all()
            if slug
        ),
    )


def _entity_and_clause(object_type: str, mode: ObjectAccess):
    """The entity and the clause one probe means — read off `OBJECT_SPECS`, never an arm per type.

    A spec with no `write_clause` answers writes on its narrowest read clause (`roster_read_clause`, else
    `read_clause`), so the fallthrough is never wider than the read. A non-`ObjectAccess` mode is refused,
    not coerced."""
    if not isinstance(mode, ObjectAccess):
        raise ValueError(f"mode must be an ObjectAccess member, got {mode!r}")
    spec = OBJECT_SPEC_BY_NAME.get(object_type)
    if spec is None:
        raise ValueError(f"unknown object_type {object_type!r}; expected one of {OBJECT_TYPES}")
    if mode is ObjectAccess.WRITE and spec.write_clause is not None:
        return spec.entity, spec.write_clause
    if spec.roster_read_clause is not None:
        # Either mode: without a `write_clause` a mutation must not use a wider clause than a read
        # (`ObjectSpec.roster_read_clause`).
        return spec.entity, spec.roster_read_clause
    return spec.entity, spec.read_clause


def visible_epic_children(
    session: Session, principal: Principal | None, parent_id: int
) -> list[Task]:
    """An epic's children, narrowed to what `principal` may see.

    Its own helper because several readers need it (detail, rollup, timeline); a child sits in its epic's
    project, so it is reached through the same scope arm."""
    return list(
        session.scalars(
            scoped_task_select(principal)
            .where(Task.parent_task_id == parent_id)
            .order_by(Task.id)
        ).all()
    )
