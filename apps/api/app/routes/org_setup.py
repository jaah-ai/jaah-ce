"""`GET /api/org/setup` — the company-setup card's status: whether the caller sees the card, and its
ordered steps with each one's status.

TWO STEP LISTS, chosen per org. An operator-created org gets seven (`OPERATOR_STEPS`, `done`/`todo`
only). A SELF-SERVICE org — one with an `org_signups` row — gets nine (`SELF_SERVICE_STEPS`): the plan, the
operator's approval and the plan's node join the list, and every step after approval reads `blocked` "After
approval" while the org is pending (`_self_service_steps`). An org whose sign-up confirmed its plan
(`org_signups.plan_confirmed_at`, the multi-step sign-up) gets eight: no `choose_plan`.

An AI account is done once one is SIGNED IN (`_signed_in_accounts`), not merely added: a subscription account's
session lives in a node's config dir, so a deleted node takes it along.

Adding a project and giving it somewhere to run are two steps: `add_project` is "a project
exists", `add_slots` is "one can take work" — assigned to a live node (no slot needs declaring) —
and the org has a live node.

THE ONBOARDING LATCH (D15): the first time every step the console counts reads done (`FINISHING_STEPS`),
`setup_status` stamps
`orgs.onboarding_completed_at`. Hiding the card does NOT (its ✕ only hides it, and the sidebar's
Setup button brings it back until setup is done). While it is unset a new node or project is assigned
everything (`app/slot_allocation.py::auto_assign`); once set it is never cleared.

Every step is DERIVED from org state, never stored and never click-to-tick, and it is ORG-WIDE: each
signal is one `EXISTS` on the caller's org, deliberately not grant-scoped, so every admin of the org gets
the same `steps` (only booleans and one project id leave). Only the hide (the card's ✕) is per-user, and it is a
preference (`app/preferences.py::UserPreferences.company_setup_dismissed`), not part of this response.

`@always(Tenancy.ORG_SCOPED)` because the verdict is total: a caller who may not see the card is told so
with a 200, never a 403. The guard therefore resolves no principal and binds no org, so the handler
enters the principal's own org itself (`seam.bound_to`) for its reads.

Step 1 is done once any Org Owner/Admin has a git connection on any forge — GitHub, GitLab,
Bitbucket or Azure DevOps, connected or a pasted token. Its `app` fact is the one exception to "a signal on the
caller's org" and only words the wizard's Connect GitHub button: the GitHub apps are the platform operator's,
one of each kind per deployment and owned by no tenant, so `app` means one is configured
(`git_access.app_config()`). A tenant's own `github_app`/`github_install_app` row never counts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app import edition_features, git_access, org_limits, pool_state
from app.account_auth import AUTH_KIND_API_KEY
from app.account_fold import AUTH_REJECTED_QUOTA, NODE_STATUS_NO_CREDENTIALS, RECENT_REPORT_SECONDS
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.principal import Principal, current_principal, effective_access
from app.authz.visibility import readable_account_clause
from app.db import SessionLocal
from app.models.credentials import Account, AccountObservation, UserGitConnection
from app.models.identity import AdminUser
from app.models.node_groups import NodeGroupCapacity, NodeGroupProject, NodeProject
from app.models.nodes import Node, NodeOrder
from app.models.orgs import ORG_ACTIVE, ORG_PENDING, Org, OrgSignup
from app.models.projects import Project
from app.models.tasks import Task
from app.task_lifecycle import DONE, DRAFT, READY
from app.tenancy import seam
from app.tenancy.ids import is_tenant_org

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org", tags=["org-setup"])

#: An operator-created org's steps, in operator order (decision D1). Ids and order are a contract with the
#: web card and its end-to-end test.
OPERATOR_STEPS = (
    "connect_github", "add_compute", "add_account", "add_project", "add_slots", "onboard_repo", "first_task",
)

#: A self-service org's steps (decision D17): plan and approval sit before everything approval unlocks.
#: Compute comes first after approval (a subscription account signs in on a node), then the account, then
#: slots.
SELF_SERVICE_STEPS = (
    "connect_github", "add_project", "choose_plan", "approval",
    "compute", "add_account", "add_slots", "onboard_repo", "first_task",
)

#: The steps whose completion finishes onboarding: the ones the console counts (`apps/web/src/lib/welcomeWizard.ts`
#: folds either list into them — approval rides the compute step, `add_slots` is automatic, the repo's
#: preparation rides the first task). The latch closes on THESE, so the console's 100% and the
#: end of onboarding are one moment. `choose_plan` is counted by the console but not here: the console reads it
#: done once a node is under way, and a live node (which `compute` asks) answers it.
FINISHING_STEPS = frozenset({"connect_github", "add_project", "add_compute", "compute", "add_account", "first_task"})


#: What a step blocked by a pending org says in place of its link.
AFTER_APPROVAL = "After approval"

#: What `add_account` says while enabled subscription accounts hold no session on any live node (`_signed_in_accounts`),
#: followed by their names (`_sign_in_note`), so an owner with several knows which.
SIGN_IN_PREFIX = "Sign in: "

#: How many names the sign-in note lists before "+N".
SIGN_IN_NAMES_SHOWN = 3

StepStatus = Literal["done", "todo", "blocked", "in_progress", "failed"]


class LoginTarget(BaseModel):
    #: The account's slug and the node's uuid, as the Accounts page's login route takes them.
    slug: str
    node_id: str


class SetupStep(BaseModel):
    id: str
    status: StepStatus
    #: Words shown with the status (`"Waiting for approval"`, a node product's error); None for none.
    note: Optional[str] = None
    #: `add_account`'s one sign-in to do, when exactly one account needs one and one live node exists: the card links
    #: straight to it (`accountLoginPath`) instead of the Accounts list. None otherwise. (Named `login`: the API
    #: reference forbids sign-in vocabulary.)
    login: Optional[LoginTarget] = None


class GithubState(BaseModel):
    #: The deployment's GitHub OAuth App is configured — the platform operator's, not the org's.
    app: bool
    #: An Org Owner/Admin of this org has connected a git account, on any forge: what finishes step 1.
    user_connected: bool


class FirstTaskOut(BaseModel):
    """The org's first task (`_first_tasks`: any visible, non-advisor task that has started): its most recent
    `done` one when any finished, else its most recent one."""

    id: int
    title: str
    state: str


class SetupStatus(BaseModel):
    visible: bool
    steps: Optional[list[SetupStep]]
    #: What the card needs to pick step 1's per-viewer wording; None when not visible.
    github: Optional[GithubState] = None
    #: The project `onboard_repo` links to (the org's first qualifying one); None when there is none.
    project_id: Optional[int] = None
    #: The project `add_slots` links to: the org's first one, assigned or not; None for none.
    assign_project_id: Optional[int] = None
    #: The org signed itself up (it has an `org_signups` row), so it gets `SELF_SERVICE_STEPS`.
    self_service: bool = False
    #: The task the `first_task` step counts — the newest `done` one, else the newest — so a guide can follow
    #: it to `done` and never regresses once one finished; None for none.
    first_task: Optional[FirstTaskOut] = None
    #: A task the `first_task` step would count is filed and queued (`ready`) but has not started — so a guide
    #: waiting on the node says it starts then, rather than ask for one.
    task_queued: bool = False
    #: The onboarding latch (`orgs.onboarding_completed_at`, `close_onboarding`) is still unset.
    onboarding_open: bool = False


def company_setup_visible(principal: Principal | None) -> bool:
    """The card's audience: a member of a TENANT org who is its Org Owner/Admin (`Principal.is_org_admin`;
    operator decision D2's "an admin the card is for").

    Never the operator plane, and never a principal-less caller (a node token)."""
    # `org_id is None` first: `is_tenant_org(None)` is True (fail-closed there), but an unbound caller has no
    # org whose progress this could report.
    if principal is None or principal.org_id is None or not is_tenant_org(principal.org_id):
        return False
    return principal.is_org_admin


def _exists(session, statement) -> bool:
    return bool(session.scalar(select(statement.exists())))


def _qualifying_project_ids(org_id: UUID):
    """The org's qualifying projects (half of `add_slots`, see `_slots_ready`): those
    assigned to a live node — explicitly or through the node's group, as
    `slot_allocation.effective_assignments` reads it. Live means what dispatch means (`pool_state.live_node_clause`)."""
    live = (pool_state.not_a_vnode_host(), *pool_state.live_node_clause(pool_state.max_observation_age_secs()))
    # Each EXISTS sits directly under the project select, so `Project.id` correlates rather than cross-joins.
    explicit = (
        select(NodeProject.id)
        .join(Node, Node.id == NodeProject.node_id)
        .where(NodeProject.project_id == Project.id, *live)
    )
    grouped = (
        select(NodeGroupProject.project_id)
        .join(NodeGroupCapacity, NodeGroupCapacity.group_id == NodeGroupProject.group_id)
        .join(Node, Node.node_group_capacity_id == NodeGroupCapacity.id)
        .where(NodeGroupProject.project_id == Project.id, *live)
    )
    return select(Project.id).where(
        Project.org_id == org_id,
        or_(explicit.exists(), grouped.exists()),
    )


def _platform_app_configured() -> bool:
    """Whether a deployment GitHub app is configured. `app_config()` detaches and opens its own
    operator-scoped session, so it runs before the org bind.

    A row whose config cannot be opened (`ValueError` from `Credential.config`) reads as "not configured"
    rather than a 500: the card draws nothing on a failed status, so one broken App row would otherwise
    hide the whole card from every tenant."""
    try:
        git_access.app_config()
    except git_access.GitAccessError:
        return False
    except ValueError:
        log.warning("org-setup: the GitHub app credential could not be read; step 1 reports no App")
        return False
    return True


def _step(step_id: str, done: bool) -> SetupStep:
    return SetupStep(id=step_id, status="done" if done else "todo")


def _add_node_step(step_id: str, done: bool) -> SetupStep:
    """The add-a-node step, blocked with the reason while the org can add no node, so the card says
    why instead of linking to a dialog that would refuse it."""
    refusal = None
    # The community edition launches no cloud nodes: nothing refuses the add.
    if edition_features.enabled("cloud_nodes"):
        from app import cloud_node_launch

        refusal = None if done else cloud_node_launch.node_add_refusal()
    return SetupStep(id=step_id, status="blocked", note=refusal) if refusal else _step(step_id, done)


def _signals(session, org_id: UUID, *, app: bool, principal: Principal | None) -> dict:
    """Every org-state signal the steps read, one query each, so the cost does not grow with the org."""
    # An org ADMIN connected a git account on ANY forge: a per-forge connection row, or the
    # pre-0291 pointer. The org role is the kernel's (`authz.effective_access`), asked per connected member
    # rather than restated in SQL; a handful of rows per org.
    connected = set(session.scalars(
        select(AdminUser.id).where(
            AdminUser.org_id == org_id, AdminUser.git_credential_id.is_not(None)
        )
    ).all()) | set(session.scalars(
        select(UserGitConnection.user_id).where(UserGitConnection.org_id == org_id)
    ).all())
    user_connected = any(effective_access(session, user_id)[1].org_role is not None for user_id in connected)
    qualifying = _qualifying_project_ids(org_id)
    first_project = session.scalar(
        select(Project.id)
        .where(Project.org_id == org_id)
        .order_by(Project.id)
        .limit(1)
    )
    node_ordering = False
    # The community edition has no node orders.
    if edition_features.enabled("cloud_nodes"):
        from app import node_orders

        node_ordering = _exists(
            session,
            select(NodeOrder.id).where(NodeOrder.org_id == org_id, NodeOrder.stage.in_(node_orders.COMING_STAGES)),
        )
    return {
        "app": app,
        "user_connected": user_connected,
        "compute": pool_state.live_node_exists(session, org_id),
        # An Add-node order the system is bringing up, so the compute step reads "Preparing your node…" meanwhile.
        "node_ordering": node_ordering,
        "account": _exists(session, _signed_in_accounts(org_id)),
        # An enabled account the viewer may read exists but none is signed in (`add_account` names them after
        # `SIGN_IN_PREFIX`). Read through the roster's clause: a colleague's personal account is never named.
        "account_added": _exists(
            session,
            select(Account.id).where(
                Account.org_id == org_id, Account.enabled.is_(True), readable_account_clause(principal)
            ),
        ),
        # Those not signed in, by name: what the sign-in note names and links to.
        "signed_out": session.execute(
            select(Account.name, Account.slug)
            .where(
                Account.org_id == org_id, Account.enabled.is_(True), readable_account_clause(principal),
                Account.id.not_in(_signed_in_accounts(org_id)),
            )
            .order_by(Account.name, Account.slug)
        ).all(),
        # Up to two live nodes: one is where that sign-in goes; two means the owner picks.
        "live_nodes": session.scalars(
            select(Node.id)
            .where(Node.org_id == org_id, pool_state.not_a_vnode_host(),
                   *pool_state.live_node_clause(pool_state.max_observation_age_secs()))
            .order_by(Node.id)
            .limit(2)
        ).all(),
        # `add_project`: any project, slots or not — `add_slots` asks the rest of it.
        "has_project": first_project is not None,
        # `add_slots`' link: that first project, assigned or not.
        "assign_project_id": first_project,
        "project_id": session.scalar(qualifying.order_by(Project.id).limit(1)),
        "onboarded": _exists(
            session,
            select(Task.id).where(
                Task.org_id == org_id,
                Task.persona == "onboard",
                Task.state == "done",
                Task.project_id.in_(qualifying),
            ),
        ),
        "first_task": _exists(session, _first_tasks(org_id)),
        "task_queued": _exists(session, _visible_tasks(org_id).where(Task.state == READY)),
    }


def _signed_in_accounts(org_id: UUID):
    """`add_account`'s accounts: enabled ones a runner could use now. An API-key account counts once enabled (its
    key is read at dispatch, on whichever node). A subscription account counts only while a LIVE node holds its
    session: that node's recent observation (inside dispatch's `RECENT_REPORT_SECONDS`) neither disables it, says
    `no_credentials`, nor carries a rejected token. So a node deleted
    and added again — whose fresh config dir holds no login — reopens the step until someone signs in."""
    signed_in = (
        select(AccountObservation.id)
        .join(Node, Node.id == AccountObservation.node_id)
        .where(
            AccountObservation.account_id == Account.id,
            pool_state.not_a_vnode_host(),
            *pool_state.live_node_clause(pool_state.max_observation_age_secs()),
            func.extract("epoch", func.now() - AccountObservation.attested_at) <= RECENT_REPORT_SECONDS,
            AccountObservation.enabled.is_not(False),
            AccountObservation.extra["status"].astext.is_distinct_from(NODE_STATUS_NO_CREDENTIALS),
            AccountObservation.quota["status"].astext.is_distinct_from(AUTH_REJECTED_QUOTA["status"]),
        )
    )
    return select(Account.id).where(
        Account.org_id == org_id, Account.enabled.is_(True), or_(Account.auth_kind == AUTH_KIND_API_KEY, signed_in.exists())
    )


def _account_step(signals: dict) -> SetupStep:
    """`add_account`: done once one is signed in; an enabled account that is not, on a live node, asks for the sign-in."""
    if signals["account"]:
        return _step("add_account", True)
    if signals["account_added"] and signals["compute"]:
        signed_out, nodes = signals["signed_out"], signals["live_nodes"]
        target = (
            LoginTarget(slug=signed_out[0].slug, node_id=str(nodes[0]))
            if len(signed_out) == 1 and len(nodes) == 1 else None
        )
        return SetupStep(id="add_account", status="todo", note=_sign_in_note(signed_out), login=target)
    return _step("add_account", False)


def _sign_in_note(signed_out) -> str:
    """`Sign in: team-max, solo` — the first `SIGN_IN_NAMES_SHOWN` names, then `+N` for the rest."""
    names = [row.name or row.slug for row in signed_out]
    shown = ", ".join(names[:SIGN_IN_NAMES_SHOWN])
    rest = len(names) - SIGN_IN_NAMES_SHOWN
    return SIGN_IN_PREFIX + (f"{shown} +{rest}" if rest > 0 else shown)


def _first_tasks(org_id: UUID):
    """The org's tasks the `first_task` step counts: any task that has STARTED (left `draft`/`ready`), whoever
    filed it — the onboard task included — but never a hidden one, nor an `advisor` one (a chat)."""
    return _visible_tasks(org_id).where(Task.state.not_in((DRAFT, READY)))


def _visible_tasks(org_id: UUID):
    """The org's tasks a setup step may count: never a hidden one, nor an `advisor` one (a chat)."""
    return select(Task.id).where(Task.org_id == org_id, Task.hidden.is_(False), Task.persona != "advisor")


def _latest_first_task(session, org_id: UUID) -> Optional[FirstTaskOut]:
    """The newest qualifying task in `done` when any exists, else the newest qualifying task."""
    newest = _first_tasks(org_id).order_by(
        (Task.state == DONE).desc(), Task.created_at.desc(), Task.id.desc()
    ).limit(1)
    row = session.execute(select(Task.id, Task.title, Task.state).where(Task.id.in_(newest))).first()
    return FirstTaskOut(id=row.id, title=row.title, state=row.state) if row is not None else None


def _operator_steps(signals: dict) -> list[SetupStep]:
    """An operator-created org's seven steps."""
    return [
        _step("connect_github", signals["user_connected"]),
        _compute_ordering("add_compute") if _node_coming(signals) else _add_node_step("add_compute", signals["compute"]),
        _account_step(signals),
        _step("add_project", signals["has_project"]),
        _step("add_slots", _slots_ready(signals)),
        _step("onboard_repo", signals["onboarded"]),
        _step("first_task", signals["first_task"]),
    ]


def _slots_ready(signals: dict) -> bool:
    """`add_slots`: a qualifying project AND a live node of the org. An assignment is enough — the step no
    longer asks for a declared slot (AC8.2)."""
    return signals["project_id"] is not None and signals["compute"]


def _node_coming(signals: dict) -> bool:
    """No live node yet, but an Add-node order is on its way."""
    return not signals["compute"] and signals["node_ordering"]


def _compute_ordering(step_id: str) -> SetupStep:
    return SetupStep(id=step_id, status="in_progress", note="Preparing your node…")


def _compute_step(signals: dict, node: str, node_note: Optional[str]) -> SetupStep:
    """The compute step of an ACTIVE self-service org: its plan-included node's state, or today's Add compute.

    Any live node of the org finishes it — one the owner added themselves counts as much as one the plan includes."""
    if signals["compute"]:
        return SetupStep(id="compute", status="done", note="Ready" if node == "ready" else None)
    if node == "provisioning" or signals["node_ordering"]:
        return _compute_ordering("compute")
    if node == "failed":
        return SetupStep(id="compute", status="failed", note=node_note)
    # `none`, `not_configured`, `removed`, and a `ready` whose node has since gone quiet: add one yourself.
    return _add_node_step("compute", False)


def _self_service_steps(session, org: Org, signup: OrgSignup, signals: dict) -> list[SetupStep]:
    """A self-service org's nine steps (D17), eight when its sign-up confirmed the plan (no `choose_plan`).
    Until the org is approved every step after `approval` reads
    `blocked` "After approval", so the card never offers a link the server would refuse. An org the system
    approved at sign-up (`app/org_approval.py::approve_at_signup`) says so on its approval step."""
    pending = org.lifecycle == ORG_PENDING
    if org.lifecycle == ORG_ACTIVE:
        automatic = False
        # The community edition has no sign-up approval.
        if edition_features.enabled("signup"):
            from app import org_approval

            automatic = org_approval.is_auto_approved(org, signup)
        approval = SetupStep(id="approval", status="done", note="Approved automatically" if automatic else "Approved")
    elif pending:
        approval = SetupStep(id="approval", status="in_progress", note="Waiting for approval")
    else:
        # Rejected: its users are refused at sign-in (`org_gate.refuse_unapproved_sign_in`); here for totality.
        approval = SetupStep(id="approval", status="failed", note="Not approved")
    steps = [
        _step("connect_github", signals["user_connected"]),
        # "A project exists": compute comes later in this order, so a live slot cannot be asked of it.
        _step("add_project", signals["has_project"]),
        approval,
    ]
    if signup.plan_confirmed_at is None:
        # An org the new sign-up created chose its plan there (PRD D8): no Choose plan step.
        steps.insert(2, _step("choose_plan", org.plan_chosen_at is not None))
    if org.lifecycle != ORG_ACTIVE:
        after_approval = SELF_SERVICE_STEPS[SELF_SERVICE_STEPS.index("approval") + 1:]
        return steps + [SetupStep(id=i, status="blocked", note=AFTER_APPROVAL) for i in after_approval]
    # The tenant's reading, as `routes/auth.py::_me_org_ref` serves it: a failure's operator detail (which
    # can name the operator's Multi-Node Host and its slot counts) is not the tenant's to read.
    node: str = "none"
    node_note: Optional[str] = None
    # The community edition has no plans bundles: no node product, none owed.
    if edition_features.enabled("billing"):
        from app import plan_bundles

        node, node_note, _node_name = plan_bundles.first_node(session, org.id, tenant=True)
    if not signals["account"] and not signals["compute"] and (node == "provisioning" or _node_coming(signals)):
        # A subscription account needs a node to sign in on; an API-key account does not, so the link stays.
        account = SetupStep(id="add_account", status="in_progress", note=org_limits.AWAITING_NODE)
    else:
        account = _account_step(signals)
    return steps + [
        _compute_step(signals, node, node_note),
        account,
        _step("add_slots", _slots_ready(signals)),
        _step("onboard_repo", signals["onboarded"]),
        _step("first_task", signals["first_task"]),
    ]


def setup_status(session, org_id: UUID, *, app: bool, principal: Principal | None) -> SetupStatus:
    """The visible card for `org_id`: its step list (operator-created or self-service) and the two facts
    the card words per viewer.

    `app` is `_platform_app_configured()`, computed by the caller before it binds `org_id`; `principal` is the
    viewer, whose roster reach bounds the accounts AI account names."""
    signals = _signals(session, org_id, app=app, principal=principal)
    # `select`, never `session.get`: the read seam scopes a select, and a PK get would bypass it.
    signup = session.scalar(select(OrgSignup).where(OrgSignup.org_id == org_id))
    org = session.scalar(select(Org).where(Org.id == org_id))
    steps = (
        _self_service_steps(session, org, signup, signals) if org is not None and signup is not None
        else _operator_steps(signals)
    )
    first_task = _latest_first_task(session, org_id) if signals["first_task"] else None
    if all(step.status == "done" for step in steps if step.id in FINISHING_STEPS):
        close_onboarding(session, org_id)
    return SetupStatus(
        visible=True,
        steps=steps,
        github=GithubState(app=signals["app"], user_connected=signals["user_connected"]),
        project_id=signals["project_id"],
        assign_project_id=signals["assign_project_id"],
        self_service=signup is not None,
        first_task=first_task,
        task_queued=signals["task_queued"],
        onboarding_open=org is not None and org.onboarding_completed_at is None,
    )


def close_onboarding(session, org_id: UUID) -> None:
    """Stamp the onboarding latch (D15) if it is still open, and commit. Never cleared, so a second call —
    every later status read of a finished org, a second dismissal — writes nothing."""
    org = session.scalar(select(Org).where(Org.id == org_id))
    if org is None or org.onboarding_completed_at is not None:
        return
    org.onboarding_completed_at = datetime.now(timezone.utc)
    session.commit()


@router.get("/setup", response_model=SetupStatus)
@always(Tenancy.ORG_SCOPED)
def get_setup_status(principal: Principal | None = Depends(current_principal)) -> SetupStatus:
    """The card's verdict and, when visible, the org's progress."""
    org_id = principal.org_id if company_setup_visible(principal) and principal is not None else None
    if org_id is None:
        return SetupStatus(visible=False, steps=None)
    app = _platform_app_configured()
    with seam.bound_to(org_id), SessionLocal() as session:
        return setup_status(session, org_id, app=app, principal=principal)
