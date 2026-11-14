"""Agent-route authentication and authorization: who the calling node is, which task it declares, and what it may touch."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Annotated, Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import carryover, edition_features, node_credentials, org_quotas
from app.db_async import get_db, release
from app.tenancy import seam
from app.task_lookup import get_task_or_404
from app.node_identity import node_id_or_none, wire_node_id_or_404
from app.models.nodes import Microvm
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
# Task transitions go through the watermark-gated helpers; the state machine single-homes in routes/tasks/.
from app.entity_shape import get_or_404

if TYPE_CHECKING:
    from app.authz.principal import Principal
    from app.models.identity import AdminUser

log = logging.getLogger("agent-routes")


def _authenticate_agent(
    x_agent_token: str,
    session: Session,
) -> node_credentials.AgentCaller:
    """Authenticate an agent → API call and say who it was. The door every `/api/agent/*` route takes.

    Wrapped by `_check_token` (the bind must happen on the event loop). Parameters are plain: a
    `Depends(...)` default on an unresolved function would bind the marker object as the session. Returns an
    `AgentCaller` so routes derive the caller; resolution lives in `app/node_credentials.py`.

    Fail closed: unrecognised token → 401; no node row holds any credential → 503 (misconfiguration).
    `ALLOW_INSECURE_AGENT` is the one local-dev opt-in (`app/dev_bypass.py`).
    """
    caller = node_credentials.resolve(session, x_agent_token)
    if caller is not None:
        return caller
    if node_credentials.auth_unconfigured(session):
        raise HTTPException(
            status_code=503,
            detail=(
                "agent auth not configured: register a node (POST /api/nodes) to issue it a token, "
                "or set ALLOW_INSECURE_AGENT=1 for local dev"
            ),
        )
    raise HTTPException(status_code=401, detail="invalid agent token")


#: The arms that read fleet-wide, enumerated, never derived: `cp_self` (this CP on loopback) and
#: `insecure` (local-dev bypass, refused at boot outside a local stack). Dropping `cp_self` silently stops
#: the CP's own sweeps; an arm not named here is unbound and logs at ERROR. `pool_tick`'s pending-intent
#: select additionally serves nothing to a caller proving no node (intents carry secrets).
_FLEET_WIDE_ARMS = frozenset(
    {
        node_credentials.ARM_CP_SELF,
        node_credentials.ARM_INSECURE,
    }
)


def _read_scope_for(caller: node_credentials.AgentCaller) -> "UUID | object | None":
    """The org this authenticated caller reads under — the agent plane's half of the tenancy bind.

      * Derived node with an org → that org (from the token-matching row, never claimed).
      * Arm in `_FLEET_WIDE_ARMS` → `OPERATOR`. Any other node-less arm is unbound and logs at ERROR.
      * Derived node with no `org_id` → unbound, loudly (unreachable with NOT NULL; kept fail-closed).

    The read seam then attaches `org_id == <this org>` to `_task_or_404_for_node`'s `session.get`. The
    fleet-wide arms read with no org predicate, hence `dev_bypass.assert_agent_auth_posture`. Oracle:
    `tests/test_agent_task_credentials.py`.
    """
    if caller.node_id is None:
        if caller.arm not in _FLEET_WIDE_ARMS:
            # A new arm does not inherit the widest grant by omission; fleet-wide is declared.
            log.error(
                "tenancy: agent arm %r derives no node and is not declared fleet-wide — leaving this "
                "request UNBOUND",
                caller.arm,
            )
            return None
        return seam.OPERATOR
    if caller.org_id is None:
        log.warning(
            "tenancy: node %s authenticated but carries no org_id — leaving this request UNBOUND "
            "rather than guessing an org",
            caller.node_id,
        )
        return None
    return caller.org_id


async def _authenticate_and_admit(
    x_agent_token: str, db: AsyncSession, x_agent_task_id: str = "", route: Optional[Request] = None,
) -> tuple[node_credentials.AgentCaller, Optional[UUID], Optional[int]]:
    """`_authenticate_agent`, then count the call against its org's request budget (429 when full,
    `app/org_quotas.py`), then look up a carry-over for the declared task on a route that carries one
    (`_carries`, `app/carryover.py`). The hottest path in the process: the reads run over the request's async
    session (`app/db_async.py`), and the budget's count — a blocking call on the limiter's own connection — runs in
    a thread, with the request's connection handed back first. A node-less arm carries no org, is never counted and
    is never carried over. Returns the caller, the carry-over's org and its task, or Nones.

    Hands the request's read-only connection back before returning (`db_async.release`), so the route starts
    holding nothing."""
    try:
        caller, budget = await db.run_sync(_authenticate_with_budget, x_agent_token)
        if caller.node_id is None:
            return caller, None, None
        if budget > 0:
            await release(db)
            await run_in_threadpool(org_quotas.admit_agent_call, caller.org_id, budget)
        if route is None:
            return caller, None, None
        return await db.run_sync(_carry_over_of, caller, x_agent_task_id, route)
    finally:
        await _release_quietly(db)


async def _release_quietly(db: AsyncSession) -> None:
    """`db_async.release` that never replaces the door's own answer (a 401, a 429) with a 500: a failed release
    keeps the connection until the session closes, as `app/db.py::release_route_connection` does."""
    try:
        await release(db)
    except SQLAlchemyError as exc:
        log.debug("agent auth: kept the request connection (%s)", type(exc).__name__)


def _authenticate_with_budget(session: Session, x_agent_token: str) -> tuple[node_credentials.AgentCaller, int]:
    """The caller, and the budget its org's call counts against (0: not counted; `org_quotas.agent_budget`)."""
    caller = _authenticate_agent(x_agent_token, session)
    if caller.node_id is None:
        return caller, 0
    return caller, org_quotas.agent_budget(session, caller.org_id)


def _carry_over_of(
    session: Session, caller: node_credentials.AgentCaller, x_agent_task_id: str, route: Request,
) -> tuple[node_credentials.AgentCaller, Optional[UUID], Optional[int]]:
    # Raw and before any bind, so it stamps nothing; the probe is the partial index on open rows.
    declared = x_agent_task_id.strip()
    if caller.node_id is None or not _CALLER_TASK_ID_RE.fullmatch(declared):
        return caller, None, None   # a node-less arm is never carried (`_authenticate_and_admit` returns first)
    carried_to = carryover.scope_for(session, node_id=caller.node_id, task_id=int(declared))
    if carried_to is None or not _carries(session, caller, int(declared), route):
        return caller, None, None
    return caller, carried_to, int(declared)


#: Agent routes naming their task in the BODY that a carried-over runner calls: each checks that task against
#: the declared one (`require_caller_task`) whatever the body says, so it cannot be steered to another.
_CARRIED_BODY_TASK_ROUTES = frozenset({"ingest_telemetry"})


def _carries(session: Session, caller: node_credentials.AgentCaller, task_id: int, route: Request) -> bool:
    """Does this route carry the declared task's carry-over? Only a route about that task: its path names it,
    it is a body-task route that checks it (`_CARRIED_BODY_TASK_ROUTES`), or its path names the runner session
    the carry-over was granted for (the transcript reports). Every other route keeps the node's own org, so a
    carried header buys nothing on the agent surface's org-wide reads and writes."""
    params = route.path_params
    if str(params.get("task_id", "")) == str(task_id):
        return True
    endpoint = route.scope.get("endpoint")
    if getattr(endpoint, "__name__", "") in _CARRIED_BODY_TASK_ROUTES:
        return True
    agent_session = params.get("agent_session_id")
    return bool(agent_session) and agent_session == carryover.granted_session(
        session, node_id=caller.node_id, task_id=task_id
    )


async def _check_token(
    x_agent_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
    x_agent_task_id: str = Header(default=""),
    request: Request = None,
) -> node_credentials.AgentCaller:
    """The agent-surface dependency: authenticate, then bind this request's org.

    `async def` on purpose: FastAPI runs a `def` in a worker thread on a copied context, where
    `seam.bind()` would die with the thread. Its reads run on the request's async session, the one the route's
    `DbDep` receives (FastAPI resolves `get_db` once per request), and leave it holding nothing. A second bind
    site because `authz.authz_guard` binds no principal for `INFRA` routes. The seam stamps the session on
    its first transaction (`seam.stamp_scope`).

    THE CARRY-OVER ARM (gate G3): when the node holds an open carry-over for the task its call
    declares (`X-Agent-Task-Id`) and the route is about that task (`_carries`), the request binds the task's NEW
    org instead of the node's, and records that task (`carryover.bind_request`) so every task-scoped check
    refuses any other task (`_refuse_a_node_not_holding`) and the secret routes refuse it
    (`_require_derived_node`). A direct call (no `request`) is never carried.
    """
    if not isinstance(x_agent_task_id, str):
        x_agent_task_id = ""   # a direct call, where the `Header` default is the marker object itself
    caller, carried_to, carried_task = await _authenticate_and_admit(x_agent_token, db, x_agent_task_id, request)
    carryover.bind_request(carried_task)
    scope = carried_to if carried_to is not None else _read_scope_for(caller)
    if carried_to is not None:
        log.info("tenancy: node %s carried over into org %s for task %s", caller.node_id, carried_to, carried_task)
    if scope is not None:
        seam.bind(scope)
    return caller


def caller_node_id(
    session: Session, caller: node_credentials.AgentCaller, claimed: str
) -> UUID:
    """The node this agent call is for — derived from the credential wherever the credential can say.

      * Derived caller, matching or empty claim → the derived id.
      * Derived caller, different claim → 403, never silent substitution.
      * No derived identity (CP loopback, dev bypass) → the claim, via `node_identity.wire_node_id_or_404`
        (uuid-only, 404 on a name).
    """
    claimed = (claimed or "").strip()
    if caller.node_id is None:
        return wire_node_id_or_404(session, claimed)
    if claimed and claimed != str(caller.node_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "this credential belongs to a different node than the one named in the request; a node "
                "may only act as itself"
            ),
        )
    return caller.node_id


def caller_node_ref(
    session: Session, caller: node_credentials.AgentCaller, claimed: Optional[str]
) -> Optional[str]:
    """`caller_node_id`'s sibling for routes whose node reference is a uuid string and whose `None` means
    something.

    Compares through `node_id_or_none` (uuid only: a name is no node's identity, since names are unique per
    org, not fleet-wide), so a claim that is not this credential's uuid is a 403. `None` survives when nothing
    can be derived (a node-restricted claim is then refused).
    """
    claimed = (claimed or "").strip() or None
    if caller.node_id is None:
        return claimed
    if claimed is not None and node_id_or_none(session, claimed) != caller.node_id:
        raise HTTPException(
            status_code=403,
            detail=(
                "this credential belongs to a different node than the one named in the request; a node "
                "may only act as itself"
            ),
        )
    # The UUID, always: the proven value, not a spelling that merely resolved to it.
    return str(caller.node_id)


def project_of_task(session: Session, task: Task) -> Project:
    """The caller's own project, or a 409 naming the reason there is none.

    Both arms guard states the schema currently forbids (`project_id` is NOT NULL with an FK). 409, not
    404 (the caller named a task, not a project) and not an assert (a data conflict, not a broken CP)."""
    if task.project_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"task {task.id} names no project",
        )
    project = session.get(Project, task.project_id)
    if project is None:
        # Unconstructible today (FK); a 409 for the reason above.
        raise HTTPException(
            status_code=409,
            detail=f"task {task.id} names project {task.project_id}, which no longer exists",
        )
    return project


def _wire_project(session: Session, project_id: Optional[int]) -> Optional[Project]:
    """The project an agent-plane request names by `project_id`, or None: no id sent, or the id names no row
    the caller can read (the id is read through the session, so the read seam narrows it to the caller's
    tenant). Each route words its own 404.

    The id is the identity (`app/project_identity.py`); the `project` display name beside it on the wire is
    never resolved — names are unique per org, not fleet-wide."""
    if project_id is None:
        return None
    return session.scalar(select(Project).where(Project.id == project_id))


# The header every task-creating route requires. A runner sends its own task id (`$CONTROL_TASK_ID`,
# exported by apps/jaahd/fleet_dispatch.py `build_auth_lines`); a host producer sends `host`.
_CALLER_TASK_HEADER = "X-Agent-Task-Id"


# The one non-numeric header value: an AFFIRMATIVE "host producer, no Task behind this call" — never an
# omission (coding-standards.md § absent must DENY). `CONTROL_TASK_ID` is set only inside a runner.
HOST_PRODUCER_CALLER = "host"


# Task id on the wire: ASCII digits, ≤9 wide (inside the `int4` `tasks.id` ceiling 2_147_483_647, so an
# over-wide value is a 4xx, not a psycopg 500). Strict on purpose — lenient `int()` accepts "+12",
# "1_2", "-5", non-ASCII digits and leading zeros, and an attribution key needs exactly one spelling.
_CALLER_TASK_ID_RE = re.compile(r"0|[1-9][0-9]{0,8}")


#: The same nine-digit ceiling as a NUMBER, for a typed task-id field: pydantic parses any width and a
#: value past int4 raises `NumericValueOutOfRange` at the database — a 500 where a 422 belongs.
MAX_WIRE_TASK_ID = 999_999_999


def require_caller_task(
    session: Session, caller: node_credentials.AgentCaller, x_agent_task_id: str, task_id: object
) -> None:
    """Refuse a node's call about a task unless it is the caller's own (white-box H1). Two walls:

      * NODE — the task is not held on another node (`_refuse_a_node_not_holding`), so a node's token
        cannot reach a task running on another node of its org. A task NO node holds passes (a runner may
        still be reporting after release) — `_task_or_404_for_node`'s gap, WB-H1U in the risk register.
      * DECLARED TASK — `X-Agent-Task-Id` names this task. jaahd stamps it from the peer, so a runner's
        call always declares its own task and a mismatch is a misrouted request. Behind the daemon's
        per-pane check and NOT node-proof (a node-token holder declares any id); two runners on one node
        are separated only by the daemon. `host` passes: only
        jaahd's own calls send it (runner-session at launch, attachment fetch at dispatch, the telemetry
        CLI) — `cred_broker` stamps a peer with no task `unidentified-peer`, never `host`.

    Absent, malformed or another task → 404, never 403. A caller naming no node (CP-self, dev bypass) is not
    narrowed — `_task_or_404_for_node`'s gap. Reads no `Task` row (`_task_or_404_for_node` explains why).
    """
    if caller.node_id is None:
        return
    _refuse_an_undeclared_task(x_agent_task_id, task_id)
    target = str(task_id).strip()
    if _CALLER_TASK_ID_RE.fullmatch(target):
        _refuse_a_node_not_holding(session, caller, int(target))


def _refuse_an_undeclared_task(x_agent_task_id: str, task_id: object) -> None:
    """404 unless the declared `X-Agent-Task-Id` is `host` or names `task_id` (`require_caller_task`)."""
    declared = x_agent_task_id.strip()
    if declared != HOST_PRODUCER_CALLER and not (
        _CALLER_TASK_ID_RE.fullmatch(declared) and declared == str(task_id).strip()
    ):
        log.warning("task-scoped call refused: %s=%r does not name task %s",
                    _CALLER_TASK_HEADER, declared[:40], task_id)
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")


async def _check_token_for_declared_task(
    task_id: int,
    x_agent_task_id: str = Header(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
) -> node_credentials.AgentCaller:
    """`require_caller_task`'s DECLARED-TASK wall alone, for the attachment fetch: jaahd (`host`) reads a
    prerequisite's or the epic's files at dispatch, and a prerequisite may still hold its slot on another
    node, so the node wall would refuse a legitimate dispatch. Not node-narrowed — WB-H1U
    (`_check_token_for_live_task` adds the live-hold wall on top of it)."""
    if caller.node_id is not None:
        _refuse_an_undeclared_task(x_agent_task_id, task_id)
    return caller


async def _check_token_for_task(
    task_id: int,
    x_agent_task_id: str = Header(default=""),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    db: AsyncSession = Depends(get_db),
) -> node_credentials.AgentCaller:
    """`_check_token`, then `require_caller_task` against the route's `task_id` (path or query)."""
    try:
        await db.run_sync(require_caller_task, caller, x_agent_task_id, task_id)
    finally:
        await release(db)
    return caller


async def _check_token_for_live_task(
    task_id: int,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_declared_task),
    db: AsyncSession = Depends(get_db),
) -> node_credentials.AgentCaller:
    """The declared-task wall, then `_require_live_hold`: for the routes that serve a RUNNING agent (the SDK's
    `agent.*` surface and its bundle), which a node may reach only while it holds the task."""
    try:
        await db.run_sync(_require_live_hold, caller, task_id)
    finally:
        await release(db)
    return caller


async def _agent_door(
    x_agent_task_id: str = Header(default=""),
    agent: node_credentials.AgentCaller = Depends(_check_token),
) -> tuple[node_credentials.AgentCaller, str]:
    """`(agent, x_agent_task_id)`: the authenticated caller and the raw `X-Agent-Task-Id` it declared, the pair
    every door without a task in its path starts from. The header is read here only; what it must name is
    each door's own wall (`live_agent_caller`, `caller_and_launcher`, `require_caller_task`, ...), run in the
    handler, so the 401 (`_check_token`) still comes before any 422 on the header."""
    return agent, x_agent_task_id


#: `door: AgentDoor` in a handler; spread into the pair-taking walls: `caller_and_launcher(session, *door)`.
AgentDoor = Annotated[tuple[node_credentials.AgentCaller, str], Depends(_agent_door)]


def _guard_task_creating_caller(x_agent_task_id: str, session: Session) -> Optional[Task]:
    """Resolve who is creating: the caller's own row, or None for the `host` producer.

    Every persona may use every creation shape (`persona_registry.TASK_CREATION_LICENCE`); what a call may
    reach is each door's own bound, and a door that makes work run unattended adds the loop guard
    (`require_human_started`). Fails closed on `X-Agent-Task-Id`: absent/malformed → 422, unknown → 404; only
    `host` and a real task id pass. The identity is self-declared, so a forged `host` is covered by the charter.
    """
    declared = x_agent_task_id.strip()
    if declared == HOST_PRODUCER_CALLER:
        return None                                     # affirmatively declared: no Task behind this call
    if not _CALLER_TASK_ID_RE.fullmatch(declared):
        # Logged CP-side: a node whose jaahd predates the header lands here, and the deploy-skew
        # window would otherwise be invisible from the control plane.
        log.warning("task-creation refused: undeclared/malformed caller %r", declared[:40])
        raise HTTPException(
            status_code=422,
            detail=(
                f"{_CALLER_TASK_HEADER} is required and must be your own task id (digits); a "
                f"non-task producer sends its own documented sentinel instead — got {declared[:40]!r}"
            ),
        )
    caller_id = int(declared)
    return get_or_404(
        session, Task, caller_id, detail=f"{_CALLER_TASK_HEADER} names task {caller_id}, which does not exist"
    )


def _require_live_hold(session: Session, caller: node_credentials.AgentCaller, task_id: int) -> None:
    """409 unless the caller's node holds `task_id` NOW: a leased pool slot of it, or its live MicroVM. Closes
    `_task_or_404_for_node`'s no-holder gap (WB-H1U) for the routes that serve a RUNNING agent — the SDK's
    `agent.*` surface and its bundle — so a node cannot read or act on an unheld (queued, settled, released)
    task of its org. A node-less caller (CP-self, dev bypass) holds nothing, stricter than `_git_task_or_404`:
    no such caller runs an agent. Run as a dependency (`_check_token_for_live_task`) before any `Task` read,
    so another node's task and an unknown one are the same 409 as an unheld one."""
    if caller.node_id is not None and (
        caller.node_id in _task_holder_node_ids(session, task_id)
        or _task_has_live_vm_on(session, task_id, caller.node_id)
    ):
        return
    log.warning("task-scoped call: refused task=%s to node=%s (it holds no live lease on it)", task_id, caller.node_id)
    raise HTTPException(status_code=409, detail=f"task {task_id} is not held by this node; only its live runner may call")


def _task_has_live_vm_on(session: Session, task_id: int, node_id) -> bool:
    """A MicroVM task holds no worktree: its runner is its live VM on the calling node (without this, every
    VM task's git push was refused here as `task not found`)."""
    if not edition_features.enabled("microvm"):
        return False  # The community edition has no MicroVMs.
    from app import microvm_placement

    return session.scalar(
        select(Microvm.id).where(Microvm.task_id == task_id, Microvm.node_id == node_id,
                                 Microvm.state.in_(microvm_placement.LIVE))
        .limit(1)
    ) is not None


def _task_holder_node_ids(session: Session, task_id: int) -> set:
    """The node(s) a task's runner may be on: its held pool slot(s), plus an open carry-over's node (`app/carryover.py`): its slot row stayed in the old org, invisible under the task's.
    """
    held = set(session.scalars(select(Worktree.node_id).where(Worktree.task_id == task_id)).all())
    # Only a carried request needs the carry-over node: under the node's own org (or the operator plane) the
    # slot row above already names it.
    if carryover.request_task() is not None:
        held |= carryover.holder_node_ids(session, task_id)
    return held


def _task_or_404_for_node(
    session: Session, caller: node_credentials.AgentCaller, task_id: int
) -> Task:
    """Resolve `task_id`, and refuse it to a node that is not the one running it.

    The CP-side backstop for every caller (`settle_task`, `_git_task_or_404`, `list_task_credentials`,
    `get_task_credential`): each takes `task_id` from the path, and the daemon's peer-derived substitution
    (`cred_broker._perform_call`) must not be the only wall.

    Deliberate gaps:
      * A caller naming no node (CP-self, dev bypass; `_FLEET_WIDE_ARMS`) is not narrowed and reads under
        `OPERATOR`, any tenant. Bounded by `assert_agent_auth_posture` and `cp_self`'s HMAC under
        `SESSION_SECRET`. The two task-credential routes lack `_require_derived_node` unlike their secret
        siblings; adding it is a deferred behaviour change.
      * A task holding no worktree is not refused (a runner may still be reporting after release).

    Cross-org (gate G3) is closed by the read seam: `session.get` misses the identity map, so
    `seam.read_seam` attaches `Task.org_id == <caller's org>` from `nodes.org_id`. Registered as
    `seam-on-miss` in the tenancy bypass census; an earlier `session.get(Task, ...)` in this
    request would break it. Oracle: `tests/test_agent_task_credentials.py`.

    Cross-task narrowing within one node is `require_caller_task`'s declared-task check, behind the daemon's
    per-pane registry check, which is unforgeable by a RUNNER and is not node-proof. Nothing here should be
    read as per-task isolation. The `merged_pr`
    settle recovery arm makes slotless `system_failed` rows settleable by any node in the org (not by a
    runner); accepted. Decided over the whole holder set (`worktrees.task_id` is not unique). 404, never 403.
    """
    task = get_task_or_404(task_id, session)
    _refuse_a_node_not_holding(session, caller, task_id)
    return task


def _refuse_a_node_not_holding(
    session: Session, caller: node_credentials.AgentCaller, task_id: int
) -> None:
    """404 when `task_id` is held, and not by `caller`'s node — `_task_or_404_for_node`'s rule and gaps (a
    node-less caller and an unheld task pass), without reading the row. A carried-over request reaches its
    own task only (`app/carryover.py`): it is bound to that task's org, so any other task id is refused."""
    if caller.node_id is None:
        return
    carried = carryover.request_task()
    if carried is not None and carried != int(task_id):
        log.warning("task-scoped call: refused task=%s to node=%s (carried over for task %s only)",
                    task_id, caller.node_id, carried)
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    holder_node_ids = _task_holder_node_ids(session, task_id)
    if holder_node_ids and caller.node_id not in holder_node_ids:
        log.warning(
            "task-scoped call: refused task=%s to node=%s (task is held on node(s)=%s)",
            task_id, caller.node_id, sorted(str(n) for n in holder_node_ids),
        )
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")


def declared_task_id(
    session: Session, caller: node_credentials.AgentCaller, x_agent_task_id: str, *, not_found: str
) -> int:
    """The task id `X-Agent-Task-Id` declares, refused (404 `not_found`) unless it is well-formed, and (the task's own
    404) when the task is held on another node (`_refuse_a_node_not_holding`). The door of the toolbox routes."""
    declared = x_agent_task_id.strip()
    if not _CALLER_TASK_ID_RE.fullmatch(declared):
        raise HTTPException(status_code=404, detail=not_found)
    task_id = int(declared)
    _refuse_a_node_not_holding(session, caller, task_id)
    return task_id


def open_declared_task(
    session: Session, caller: node_credentials.AgentCaller, x_agent_task_id: str, *, persona: str, not_found: str
) -> Task:
    """The declared task (`declared_task_id`), refused (404 `not_found`) unless it is an open task of `persona`."""
    task = session.get(Task, declared_task_id(session, caller, x_agent_task_id, not_found=not_found))
    if task is None or task.persona != persona or task.state not in ("active", "awaiting_response"):
        raise HTTPException(status_code=404, detail=not_found)
    return task


def require_human_started(session: Session, caller: Task, *, what: str) -> None:
    """403 when `caller`'s chain was started by a machine (`task_ancestry.machine_origin`): the loop guard
    on every door that makes work run unattended — a running task, a schedule, a trigger, an automation.
    `what` names the refused act in the detail."""
    from app.task_ancestry import machine_origin

    reason = machine_origin(session, caller.id)
    if reason is None:
        return
    log.warning("agent: refused %s to task=%s (%s)", what, caller.id, reason)
    raise HTTPException(
        status_code=403,
        detail=(
            f"{what} is open only to a task a person started in the console, and {reason}; this stops a "
            "chain of machine-started work from feeding itself. File a draft or name it in your report."
        ),
    )


def launcher_principal(session: Session, caller: Task):
    """`(user, principal)` of the human this task acts for (`task_ancestry.launcher_user_id`), or a 403 when
    the chain names nobody. Every org-wide door (`routes/agent_org.py` and its siblings) asks the console's
    own permission keys of this principal, so a runner never out-reaches its human."""
    from app.authz.principal import principal_for_user
    from app.models.identity import AdminUser
    from app.task_ancestry import launcher_user_id

    user_id = launcher_user_id(session, caller.id)
    user = session.get(AdminUser, user_id) if user_id is not None else None
    if user is None or user.is_system:
        raise HTTPException(
            status_code=403,
            detail=(
                f"task {caller.id} resolves no human who started or created it, so it has no console authority "
                "to act with. Name it in your report."
            ),
        )
    principal = principal_for_user(session, user)
    if user.org_id != caller.org_id and not principal.is_operator:
        # The org-wide doors act inside the caller task's org; a launcher seated elsewhere (a creator from
        # another org, which `launcher_user_id` falls back to) holds no console authority there.
        log.warning("agent: refused task=%s its launcher %s (seated in another org)", caller.id, user.id)
        raise HTTPException(
            status_code=403,
            detail=(
                f"the person task {caller.id} acts for is not a member of its org, so it has no console "
                "authority there. Name it in your report."
            ),
        )
    return user, principal


def live_agent_caller(session: Session, agent: node_credentials.AgentCaller, x_agent_task_id: str) -> Task:
    """The calling task for an org-wide door: declared in `X-Agent-Task-Id`, held by THIS node, and still
    live (`routes/tasks/agent_filing.py::_require_live_filer`). No persona licence: what the call may
    reach is the launcher's own console authority (`launcher_principal`)."""
    from app.routes.tasks.agent_filing import _require_live_filer

    declared = x_agent_task_id.strip()
    if not _CALLER_TASK_ID_RE.fullmatch(declared):
        # The header value is not echoed: it is caller-supplied, and a model reads this detail.
        raise HTTPException(
            status_code=422, detail=f"{_CALLER_TASK_HEADER} is required and must be your own task id (digits)"
        )
    caller = _task_or_404_for_node(session, agent, int(declared))
    # `_task_or_404_for_node` passes an UNHELD task (its no-holder gap); a node-token holder must not declare
    # a queued task of its org and borrow that task's launcher's authority. A node-less arm (CP-self, dev
    # bypass) keeps that function's fleet-wide posture: it holds no node to check against.
    if agent.node_id is not None:
        _require_live_hold(session, agent, caller.id)
    _require_live_filer(caller)
    return caller


def caller_and_launcher(
    session: Session, agent: node_credentials.AgentCaller, x_agent_task_id: str
) -> tuple[Task, AdminUser, Principal]:
    """`(caller, launcher, principal)` for one agent door: the live task (`live_agent_caller`) and whom it acts as
    (`launcher_principal`)."""
    caller = live_agent_caller(session, agent, x_agent_task_id)
    user, principal = launcher_principal(session, caller)
    return caller, user, principal
