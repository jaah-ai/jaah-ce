"""Writing the `sessions` row — created at start, settled at end, and never at the cost of either.

The signals are ones the control plane already receives (Console is attach-only and leases nothing):

  create, fleet        `POST /api/agent/tasks/{id}/runner-session` (routes/agent/task_terminal.py)
  end,    fleet        `lease_drain._release` — the teardown intent already carries `session_id`

Every write here is best-effort and never raises into its caller — see `_in_a_savepoint`.
`create_session` is an upsert that enriches and never repoints; an empty session id creates no row.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.agent_sessions import (
    AGENT_KIND_SDK_AGENT,
    DEFAULT_AGENT_KIND,
    END_REASON_RECONSTRUCTED,
    RUNTIMES,
    STATUS_ENDED,
    STATUS_RUNNING,
)
from app import agent_providers, carryover, edition_features
from app import session_transcripts as transcripts
from app.tenancy import seam
from app.entity_shape import SYSTEM_ACTOR_ID
from app.events import emit_console_event
from app.models.agent_sessions import AgentSession
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.clock import as_utc as _as_utc, utcnow as _now

log = logging.getLogger(__name__)

# The columns an enriching create may fill. `transcript_*`, `status`, `end_reason` and `ended_at` are
# absent: a late create must not resurrect a finished session.
_ENRICHABLE = (
    "node_id", "account_id", "project_id", "task_id",
    "worktree_ref", "cwd", "branch", "holder", "pid", "proc_started_at", "parent_session_id", "agent_ref",
    "dispatched_runtime",
)

# The text members of `_ENRICHABLE` — the ones a resolver can answer `""` for.
_ENRICHABLE_TEXT = ("worktree_ref", "cwd", "branch", "holder", "parent_session_id")


def _null_the_empties(values: dict[str, Any]) -> None:
    """`""` is an absence, spelled `NULL` in the database (coding-standards.md §"Absence is not a state").

    The create paths' resolvers spell "not found" differently (`None` vs `""`); a `""` that lands first
    pins the row, since the enrich's `COALESCE('', '/real/path')` keeps `''`, and the transcript is never
    located. Normalised here so INSERT and enrich are covered together; driven by `_ENRICHABLE_TEXT`.
    Mutates in place."""
    for name in _ENRICHABLE_TEXT:
        if values.get(name) == "":
            values[name] = None


def display_name(
    *, agent_session_id: str, task_id: Optional[int], worktree_ref: Optional[str]
) -> str:
    """The `name` for a session row — what an operator reads before they open it.

    NOT NULL with no server default (models.py), so every writer owes one; the task or slot goes in front
    of the unreadable session id."""
    short = (agent_session_id or "")[:8]
    if task_id is not None:
        return f"task {task_id} · {short}"
    if worktree_ref:
        return f"{worktree_ref} · {short}"
    return short or "session"


def node_for_task(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """Which node a task's runner is executing on, read off its held slot — or None.

    The fleet create path carries no node, and a NULL `node_id` row is in no node's `/work` list. The held
    `worktrees` row (keyed on the task) is the source; `generation`/`id` DESC picks the latest. None (no
    slot) is recorded as NULL rather than guessed."""
    if task_id is None:
        return None
    held = session.scalar(
        select(Worktree.node_id)
        .where(Worktree.task_id == int(task_id), Worktree.node_id.is_not(None))
        .order_by(Worktree.generation.desc(), Worktree.id.desc())
        .limit(1)
    )
    return held


def task_slot_path(session: Session, task_id: Optional[int]) -> str:
    """The absolute worktree path a task's runner is executing in — the session row's `cwd`.

    The node locates the transcript at `<config_dir>/projects/<munge(cwd)>/<session_id>.jsonl`. Read off
    the held `worktrees` row, not `tasks.worktree_ref` (unparseable for hyphenated project names). `""`
    when the task holds no slot."""
    if task_id is None:
        return ""
    held = session.scalar(
        select(Worktree.path)
        .where(Worktree.task_id == int(task_id), Worktree.node_id.is_not(None))
        .order_by(Worktree.generation.desc(), Worktree.id.desc())
        .limit(1)
    )
    return held or ""


#: provider value -> the `agent_kind` spelling its sessions kept from before a provider was a kind. A row, not a
#: comparison against `DEFAULT_PROVIDER`: the spelling is data.
_HISTORICAL_KINDS: dict[str, str] = {agent_providers.DEFAULT_PROVIDER: DEFAULT_AGENT_KIND}


def agent_kind_for_provider(provider: Optional[str]) -> str:
    """The `agent_kind` of a session run on an account of `provider` (`accounts.provider`).

    Claude keeps its historical spelling; every other provider IS its kind (its catalogue `value`), so a new
    provider costs no row here. Empty reads as the default provider, as `accounts.provider` does."""
    value = (provider or "").strip() or agent_providers.DEFAULT_PROVIDER
    return _HISTORICAL_KINDS.get(value, value)


def engine_label(agent_kind: str) -> Optional[str]:
    """The catalogue label of the built-in engine that ran a session ("Claude Code"): `agent_kind_for_provider`
    read backwards. None for an `sdk_agent` run (its `agent_ref` names it) and for a kind this build does not know."""
    provider = next((value for value, kind in _HISTORICAL_KINDS.items() if kind == agent_kind), agent_kind)
    found = agent_providers.by_value(provider)
    return found.label if found is not None else None


def create_session(
    session: Session,
    *,
    agent_session_id: str,
    agent_kind: str = DEFAULT_AGENT_KIND,
    node_id: Optional[UUID] = None,
    account_id: Optional[UUID] = None,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    worktree_ref: Optional[str] = None,
    cwd: Optional[str] = None,
    branch: Optional[str] = None,
    holder: Optional[str] = None,
    pid: Optional[int] = None,
    proc_started_at: Optional[datetime] = None,
    parent_session_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    status: str = STATUS_RUNNING,
    end_reason: Optional[str] = None,
    ended_at: Optional[datetime] = None,
    created_by_id: UUID = SYSTEM_ACTOR_ID,
    agent_ref: Optional[dict[str, Any]] = None,
    dispatched_runtime: Optional[str] = None,
) -> Optional[UUID]:
    """Record that a session started. Returns the row's id, or None when nothing was written.

    Idempotent by the database: `ON CONFLICT DO NOTHING` on `uq_sessions_agent_session_id`, then an
    enrich that writes only still-NULL columns, so a second path adds what it knows without overwriting.
    Does not commit; the caller's transaction owns that."""
    if not agent_session_id:
        # Defensive: a row for `''` would be a permanent unattributable orphan.
        return None
    if not transcripts.is_usable_session_id(agent_session_id):
        # Same guard as `/sessions/observed`: an unkeyable `pending` row 422s every ticket without
        # moving and starves the node's real transcripts. Nothing upstream guarantees a uuid; loud,
        # because such an id is an upstream bug.
        log.warning(
            "sessions: refusing to record %r — a session id that cannot be a path segment cannot be "
            "keyed, and such a row would starve its node's work list rather than ever be uploaded",
            agent_session_id[:160],
        )
        return None

    started = started_at or _now()
    values: dict[str, Any] = {
        "agent_session_id": agent_session_id,
        "agent_kind": agent_kind or DEFAULT_AGENT_KIND,
        "name": display_name(
            agent_session_id=agent_session_id, task_id=task_id, worktree_ref=worktree_ref
        ),
        "node_id": node_id,
        "account_id": account_id,
        "project_id": project_id,
        "task_id": task_id,
        "worktree_ref": worktree_ref,
        "cwd": cwd,
        "branch": branch,
        "holder": holder,
        "pid": pid,
        "proc_started_at": proc_started_at,
        "parent_session_id": parent_session_id,
        "started_at": started,
        "status": status,
        "end_reason": end_reason,
        "ended_at": ended_at,
        "created_by_id": created_by_id,
        "updated_by_id": created_by_id,
        # Which SDK agent ran an `sdk_agent` session (`agent_session_lifecycle.agent_ref_for_task`); NULL otherwise.
        "agent_ref": agent_ref,
        # The runtime the dispatch chose (`fleet_verdicts.dispatch_runtime_for_task`), display only (`models.py`).
        "dispatched_runtime": dispatched_runtime if dispatched_runtime in RUNTIMES else None,
        # Named here: a Core `pg_insert` (needed for ON CONFLICT) is invisible to the write seam's
        # `stamp_org`, and `org_id` is NOT NULL. Precedence node → project → task (all exact when they
        # resolve; `AgentSession` is `Stored`, see `app/tenancy/manifest.py`), resolved in SQL inside
        # the savepoint. No well-known-org floor: with nothing resolvable the insert raises inside the
        # savepoint, costing only this row
        # (`test_tenant_create_surfaces.py::test_no_create_path_falls_back_to_a_well_known_org`).
        "org_id": func.coalesce(
            select(Node.org_id).where(Node.id == node_id).scalar_subquery(),
            select(Project.org_id).where(Project.id == project_id).scalar_subquery(),
            select(Task.org_id).where(Task.id == task_id).scalar_subquery(),
        ),
    }
    if task_id is not None:
        # The task's project-memory arm as resolved for this dispatch (`app/project_memory/switch.py`), copied
        # in SQL so the session keeps the arm it ran under after the task's next attempt re-resolves it.
        values["memory_arm"] = select(Task.memory_arm).where(Task.id == task_id).scalar_subquery()
    _null_the_empties(values)
    if project_id is not None:
        # `sessions.project_id` is keyed to the row's org (`app/tenancy/manifest.py`). A carried-over runner
        # (`app/carryover.py`) runs on an old-org node for a project that moved away, so its row (the node's
        # org) records no project rather than being refused: the project is kept only where it is the org's.
        node_org = select(Node.org_id).where(Node.id == node_id).scalar_subquery()
        values["project_id"] = select(Project.id).where(
            Project.id == project_id, Project.org_id == func.coalesce(node_org, Project.org_id)
        ).scalar_subquery()
    inserted = session.execute(
        pg_insert(AgentSession)
        .values(**values)
        # `org_id` is not in `_ENRICHABLE`: the first create decides the org; moving it would repoint.
        .on_conflict_do_nothing(constraint="uq_sessions_agent_session_id")
        .returning(AgentSession.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted

    # The row already existed — this path knows facts the other one did not. Fill ONLY the NULLs.
    fill = {name: values[name] for name in _ENRICHABLE if values.get(name) is not None}
    if "project_id" in fill:
        # Against the recorded row's own org, which the first create decided (see the insert's note).
        fill["project_id"] = select(Project.id).where(
            Project.id == project_id, Project.org_id == AgentSession.org_id
        ).scalar_subquery()
    if "memory_arm" in values:
        fill["memory_arm"] = values["memory_arm"]
    if fill:
        extra: dict[str, Any] = {}
        if agent_ref is not None and agent_kind == AGENT_KIND_SDK_AGENT:
            # A row a node sensed first was born the default kind; the dispatch's knowledge that an SDK agent
            # ran is the authority, and the node's sweep keys on it.
            extra["agent_kind"] = AGENT_KIND_SDK_AGENT
        session.execute(
            update(AgentSession)
            .where(AgentSession.agent_session_id == agent_session_id)
            # `COALESCE` in SQL, so racing enriches cannot lose a value or overwrite a recorded one.
            .values(**{name: _coalesce(name, value) for name, value in fill.items()}, **extra)
        )
    return session.scalar(
        select(AgentSession.id).where(AgentSession.agent_session_id == agent_session_id)
    )


def _coalesce(name: str, value: Any):
    """`COALESCE(<column>, :value)` — keep what is recorded, write only into a NULL. The value is bound with the
    column's own type when it is a dict, so a jsonb `agent_ref` adapts as the INSERT's does."""
    column = getattr(AgentSession, name)
    return func.coalesce(column, literal(value, type_=column.type) if isinstance(value, dict) else value)


def end_session(
    session: Session,
    *,
    agent_session_id: str,
    end_reason: str,
    ended_at: Optional[datetime] = None,
    end_detail: Optional[str] = None,
) -> bool:
    """Record that a session ended. Returns True when a row moved. Idempotent.

    Only a `running` row moves, so the first ending wins (a late teardown hook cannot replace
    `idle_timeout` with `normal`) and a hook is safe to fire twice. `end_detail` (the cause sentence) rides
    the same move, redacted and capped here so no caller can store a secret or an unbounded string; a detail
    that arrives after the end goes through `stamp_end_detail`."""
    if not agent_session_id:
        return False
    detail = _detail(end_detail)
    moved = session.execute(
        update(AgentSession)
        .where(
            AgentSession.agent_session_id == agent_session_id,
            AgentSession.status == STATUS_RUNNING,
        )
        .values(
            status=STATUS_ENDED,
            end_reason=end_reason,
            ended_at=ended_at or _now(),
            updated_by_id=SYSTEM_ACTOR_ID,
            # COALESCE: a break report stamped while the row still ran (`stamp_end_detail`) is the runner's own
            # words and outranks the attempt's summary of them.
            **({"end_detail": func.coalesce(AgentSession.end_detail, detail)} if detail else {}),
        )
    ).rowcount
    # A run on the Jaah account gets no more credentials once its session has ended (`app/jaah_routing/`).
    if edition_features.enabled("jaah_ai_credit"):
        from app.jaah_routing import sessions as jaah_sessions

        jaah_sessions.end_for_agent_session(session, agent_session_id)
    # A carry-over is granted per session: the session's end is one of its end edges (`app/carryover.py`).
    carryover.close(session, launch_session_id=agent_session_id)
    return bool(moved)


def end_task_sessions(
    session: Session, *, task_id: int, end_reason: str, end_detail: Optional[str] = None
) -> int:
    """End EVERY still-`running` session row of `task_id`; returns how many moved. Does not commit.

    A failed attempt has no single session id to end: its launch may have recorded one row and its runner
    another, and a row nobody ends reads `running` forever on Audit › Sessions. Each row goes through
    `end_session`, so it gets the same first-wins move and the same Jaah-account and carry-over end edges."""
    running = session.scalars(
        select(AgentSession.agent_session_id).where(
            AgentSession.task_id == int(task_id), AgentSession.status == STATUS_RUNNING,
        )
    ).all()
    return sum(
        end_session(session, agent_session_id=agent_session_id, end_reason=end_reason, end_detail=end_detail)
        for agent_session_id in running
    )


def stamp_end_detail(
    session: Session,
    *,
    agent_session_id: str,
    detail: str,
    task_id: Optional[int] = None,
    node_id: Optional[UUID] = None,
    replace: bool = False,
) -> bool:
    """Write the cause sentence onto a session row, whatever its status. True when it landed.

    For a runner's break report, which may arrive after its attempt already ended the row (`end_session` then
    had only the bare reason or the attempt's summary). First wins by default, as in `end_session`; `replace`
    overwrites whatever the row holds — the break route passes it, since the runner's own report is a better cause
    than the CP's summary of the attempt. `task_id` and `node_id` fence the write to that task's row on that node:
    the session id is caller-supplied, and the caller is authorised for its task (and, a node, for its own rows),
    not for whatever row the id names."""
    cleaned = _detail(detail)
    if not agent_session_id or not cleaned:
        return False
    fence = [AgentSession.agent_session_id == agent_session_id]
    if not replace:
        fence.append(AgentSession.end_detail.is_(None))
    if task_id is not None:
        fence.append(AgentSession.task_id == int(task_id))
    if node_id is not None:
        fence.append(AgentSession.node_id == node_id)
    return bool(session.execute(
        update(AgentSession).where(*fence).values(end_detail=cleaned, updated_by_id=SYSTEM_ACTOR_ID)
    ).rowcount)


# `sessions.end_detail`'s ceiling: one sentence on a list row, not a log.
END_DETAIL_MAX = 500


def _detail(detail: Optional[str]) -> Optional[str]:
    """A cause sentence safe to persist (`redacted_error`, capped), or None for an empty one."""
    cleaned = redacted_error(detail or "", END_DETAIL_MAX).strip()
    return cleaned or None


def record_observed(
    session: Session,
    *,
    agent_session_id: str,
    node_id: Optional[UUID],
    account_id: Optional[UUID] = None,
    worktree_ref: Optional[str] = None,
    cwd: Optional[str] = None,
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
) -> bool:
    """Create the row for a session the node saw and the CP never recorded. True when one was created.

    The node reports it only after its probe says it is dead, so it lands `ended`/`reconstructed`, never
    `running`; `transcript_status` stays `pending`. Node-supplied input against a global unique key, so an
    existing row is never repointed — the conflict arm only fills a NULL `node_id` (and its org)."""
    if not agent_session_id or not transcripts.is_usable_session_id(agent_session_id):
        # The route refuses both too; same guard at the writer as `create_session`.
        return False
    # Read before the write, so the return answers "a new orphan" (the page's honesty banner counts it).
    existing = session.execute(
        select(AgentSession.id, AgentSession.node_id).where(AgentSession.agent_session_id == agent_session_id)
    ).first()
    if existing is not None and existing.node_id is not None:
        # The upsert below could only no-op (its fill is `WHERE node_id IS NULL`): the node re-reports
        # every session it still sees, so skipping the write is most of this route's cost.
        return False
    existed = existing.id if existing is not None else None
    observed = pg_insert(AgentSession)
    created = session.execute(
        observed
        .values(
            agent_session_id=agent_session_id,
            agent_kind=DEFAULT_AGENT_KIND,
            name=display_name(
                agent_session_id=agent_session_id, task_id=None, worktree_ref=worktree_ref
            ),
            node_id=node_id,
            account_id=account_id,
            worktree_ref=worktree_ref,
            cwd=cwd,
            started_at=started_at or _now(),
            ended_at=ended_at,
            status=STATUS_ENDED,
            end_reason=END_REASON_RECONSTRUCTED,
            created_by_id=SYSTEM_ACTOR_ID,
            updated_by_id=SYSTEM_ACTOR_ID,
            # As in `create_session`: the node is the only reference a reconstruction has, and there
            # is no floor, so an unresolved node refuses rather than filing under the operator plane.
            org_id=select(Node.org_id).where(Node.id == node_id).scalar_subquery(),
        )
        # Fill a NULL `node_id` and nothing else: a fleet create after its lease was released writes
        # NULL, leaving the row in no node's `/work` list. The node holding the bytes is the node;
        # `COALESCE` keeps it a fill, not a repoint.
        .on_conflict_do_update(
            constraint="uq_sessions_agent_session_id",
            set_={
                "node_id": func.coalesce(AgentSession.node_id, node_id),
                # And the org that node implies (a NULL-node row may lack one). `COALESCE` keeps it a
                # fill; a no-op once the column is NOT NULL.
                "org_id": func.coalesce(AgentSession.org_id, observed.excluded.org_id),
            },
            where=AgentSession.node_id.is_(None),
        )
        .returning(AgentSession.id)
    ).scalar_one_or_none()
    # `RETURNING` fires for the fill too, so a repaired row must not count as a new orphan.
    return created is not None and existed is None


# ── The best-effort forms the four lifecycle call sites use ───────────────────────────────────────

def _sentinel_org(session: Session, agent_session_id: str):
    """The org the session sentinel is tagged with (`app/events.py::emit_console_event`): the bound org,
    else the org of the `sessions` row named by `agent_session_id`, else None.

    Not a create-path fallback: the tag decides who is woken, never where a row is filed. None is
    fail-closed (operator plane only). The reaper paths run under `operator_scope()`, so the row's org is
    what keeps the tenant's Sessions page live."""
    bound = seam.bound_org()
    if bound is not None or not agent_session_id:
        return bound
    return session.scalar(
        select(AgentSession.org_id).where(AgentSession.agent_session_id == agent_session_id)
    )


def _in_a_savepoint(session: Session, work, describe: str, agent_session_id: str) -> None:
    """Run one bookkeeping write so that its failure costs nothing but itself.

    A savepoint, not `rollback()`: callers like `set_task_runner_session` have their own uncommitted
    binding and activity note, which a full rollback would discard. A failure costs one missing or
    unsettled session row, which the node's reconciler notices."""
    try:
        # Flush the caller's pending work outside the savepoint, or an autoflush inside it would
        # swallow a caller-side failure as ours.
        session.flush()
    except Exception:  # noqa: BLE001 — not ours to swallow, but not ours to fail on either
        log.warning(
            "sessions: the caller's own pending work would not flush, so the %s of %s was not "
            "recorded; the failure belongs to the caller and is left for it to raise",
            describe, agent_session_id or "?", exc_info=True,
        )
        return
    try:
        with session.begin_nested():
            # No event unless the write landed (`work()` is truthy only when a row moved), or the
            # stream lies.
            if not work():
                return
            # Inside the savepoint, so a rolled-back write emits nothing. `id=0` is the collection
            # sentinel: `routes/stream.py` relays unprobed entities unfiltered, so a real id would leak.
            # Tagged with the session's org (`_sentinel_org`) so only that org's page and the
            # operator's wake (`routes/stream.py::_may_see_collection_sentinel`).
            emit_console_event(
                session, entity="session", id=0, action="changed",
                org_id=_sentinel_org(session, agent_session_id),
            )
    except Exception:  # noqa: BLE001 — bookkeeping may never fail a claim, a binding or a release
        log.warning(
            "sessions: recording the %s of %s failed; the session row is the only casualty and the "
            "node's reconciler is the backstop", describe, agent_session_id or "?", exc_info=True,
        )


def note_start(session: Session, *, resolve=None, **kwargs: Any) -> None:
    """`create_session`, swallowed: recording a session never blocks or delays a start.

    `resolve` exists because kwargs are evaluated at the call site, outside the savepoint: a failing
    lookup (`task_slot_path`, `node_for_task`, an `accounts` read) would take the caller's already-written
    Task update down with it. Pass lookups as a zero-argument callable returning extra kwargs; they run
    inside the nested transaction."""
    def work():
        extra = resolve() if resolve is not None else {}
        return create_session(session, **kwargs, **extra)

    _in_a_savepoint(session, work, "start", kwargs.get("agent_session_id", ""))


def note_end(session: Session, **kwargs: Any) -> None:
    """`end_session`, swallowed. A release that raised over bookkeeping would WEDGE A SLOT."""
    _in_a_savepoint(
        session, lambda: end_session(session, **kwargs),
        "end", kwargs.get("agent_session_id", ""),
    )


def note_task_sessions_end(session: Session, *, task_id: int, **kwargs: Any) -> None:
    """`end_task_sessions`, swallowed, for the same reason as `note_end`."""
    _in_a_savepoint(
        session, lambda: end_task_sessions(session, task_id=task_id, **kwargs), "end", f"task {task_id}",
    )


def project_name(session: Session, project_id: Optional[int]) -> str:
    """`projects.name` for an id, or `""`. The manifest names the project a human recognises, not an id."""
    if project_id is None:
        return ""
    return session.scalar(select(Project.name).where(Project.id == int(project_id))) or ""


def node_name(session: Session, node_id: Optional[UUID]) -> str:
    """The node's NAME for a manifest or a log line. A uuid answers "which machine?" with nothing."""
    if node_id is None:
        return ""
    return session.scalar(select(Node.name).where(Node.id == node_id)) or ""


def session_by_agent_id(session: Session, agent_session_id: str) -> Optional[AgentSession]:
    """The row for one agent session id, or None. The unique constraint makes this single-valued."""
    if not agent_session_id:
        return None
    return session.scalars(
        select(AgentSession).where(AgentSession.agent_session_id == agent_session_id)
    ).one_or_none()


# `redacted_error`'s input ceiling (see its docstring); above any caller's `limit`.
_REDACT_INPUT_MAX = 8_000


def redacted_error(message: str, limit: int = 500) -> str:
    """A `transcript_error` safe to persist — the human-readable half, with the secret half removed.

    The realistic producer is an S3-path exception carrying a presigned URL, `X-Amz-Signature` or an
    Authorization header. Everything from a URL's first `?` is dropped, and any `X-Amz-…`/`Authorization`/
    `token`-like run is replaced, including its auth scheme (`Bearer <secret>`). Control characters are
    collapsed after redaction (newlines forge logs; NUL is unstorable). The input is capped first: the
    keyword pattern is superlinear in a keyword-bearing run, on a sync handler; the cap exceeds every
    caller's `limit`."""
    cleaned = re.sub(r"(https?://[^\s?]*)\?[^\s]*", r"\1?<redacted>", (message or "")[:_REDACT_INPUT_MAX])
    cleaned = re.sub(
        # Each keyword carries `[a-z0-9_-]*` shoulders on both sides (`_` defeats `\b`; `-` and digits
        # cover hyphenated headers like `Proxy-Authorization`, `X-Auth-Token`; a leading shoulder
        # reaches keywords mid-identifier like `<AWSAccessKeyId>`). The lookbehind pins matches to an
        # identifier boundary — a ReDoS fix (quadratic without it), asserted by a timed test.
        r"(?i)(?<![a-z0-9_-])"
        r"(x-amz-[a-z0-9_-]+|[a-z0-9_-]*authorization[a-z0-9_-]*"
        r"|[a-z0-9_-]*token[a-z0-9_-]*|[a-z0-9_-]*signature[a-z0-9_-]*"
        r"|[a-z0-9_-]*secret[a-z0-9_-]*|[a-z0-9_-]*credential[a-z0-9_-]*"
        r"|[a-z0-9_-]*access[_-]?key[a-z0-9_-]*)"
        # Separator: either delimited (quotes, brackets and `<` may wrap `[:=>]+` on both sides, so
        # `{"token": ...}`, `<AWSAccessKeyId>...` and `{"X":["..."]}` all redact), or bare whitespace
        # gated on the value looking like a credential (20+ credential-alphabet chars, or an auth
        # scheme) so ordinary English like S3's `SignatureDoesNotMatch` message stays readable.
        r"(?:[\s\"'\[({<]*[:=>]+[\s\"'\[({<]*"
        r"|\s+(?=(?:bearer|basic|token|aws4-hmac-sha256)\s|[A-Za-z0-9+/=_.~-]{20,}(?:[\s\"'<>,}\])]|$)))"
        # The value ends at its own delimiter, not the next space, so a structure stays readable.
        # Known leak, left as a follow-up: a nested key (`{"credentials": {"SecretAccessKey": ...}}`)
        # is taken as the value. Vetoing that via an atomic run leaked far worse and is not used.
        r"(?:(?:bearer|basic|token|aws4-hmac-sha256)[\s\"']+)?[^\s\"'<>,}\])]+",
        r"\1=<redacted>",
        cleaned,
    )
    cleaned = "".join(c if c >= " " and c != "\x7f" else " " for c in cleaned)
    return cleaned[:limit]


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Read a possibly-naive datetime as UTC. Postgres hands back aware values; a caller may not."""
    if value is None:
        return None
    return _as_utc(value)
