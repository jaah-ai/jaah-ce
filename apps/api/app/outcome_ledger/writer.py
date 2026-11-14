"""The outcome ledger's only writer. Every seam calls one of these; no caller writes `task_outcomes`,
`task_outcome_heads` or `outcome_links` directly (Architecture §5, pinned seam 1).

Each call runs in a SAVEPOINT of the caller's transaction and never commits: the ledger is a record of the
dispatch, never a reason for the edge it records to fail, so an error here is logged and swallowed (the
`outcome_ledger_tick` catch-all closes what a lost write left open). Every state move goes through
`states.check` against `states.TRANSITIONS`.
"""
from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional, TypeVar, Union
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import persona_registry
from app.clock import utcnow as _now
from app.models.agent_sessions import AgentSession
from app.models.credentials import Account
from app.models.merge_lane import MergeLaneRow
from app.models.nodes import Node
from app.models.outcomes import OutcomeLink, TaskOutcome, TaskOutcomeHead
from app.models.task_extras import TaskPr, TaskPrChanges, TaskPrStats
from app.models.tasks import Task
from app.outcome_ledger import dimensions, states

log = logging.getLogger("outcome-ledger")

#: Personas whose dispatch delivers code: their row is judged on its merge. Every other pipeline persona's row
#: settles `settled` and, under an epic, inherits its build siblings' result (D5).
BUILD_PERSONAS: frozenset[str] = frozenset(
    persona for persona, criterion in persona_registry.PERSONA_DONE_CRITERION.items()
    if criterion == persona_registry.DONE_CRITERION_MERGED_PR
)
#: Personas a dispatch writes a row for (D1): the pipeline personas that reach a runner. `epic` never does.
#: A persona with no terminal call (`advisor`, the operator's chat) has no result to judge, so it gets no row.
LEDGER_PERSONAS: frozenset[str] = frozenset(
    persona for persona in persona_registry.PIPELINE_PERSONAS
    if persona in persona_registry.PERSONA_CHARTER and persona in persona_registry.PERSONA_TERMINALS
)
#: The `end_attempt` reason prefix a runner's own `task_fail` carries; every other attempt end is `abandoned`.
END_FAILED = "failed"
END_ABANDONED = "abandoned"
END_SETTLED = "settled"

EXCLUDE_DELETED = "deleted"
EXCLUDE_MOVED = "moved"
EXCLUDE_RESET = "reset"

#: The `pushed_shas` cap a head keeps (the daemon sends at most this many).
MAX_PUSHED_SHAS = 250
_COST_STATUS = {"measured": "captured", "partial": "partial", "unavailable": "not_captured"}

_F = TypeVar("_F", bound=Callable[..., Any])
#: A seam passes the ORM row or its id; each public function resolves it once (`_task`). Typed `Any` on the public
#: functions so the resolved row can take the parameter's name.
TaskRef = Union[Task, int]


def _ledger_write(fn: _F) -> _F:
    """Run `fn` in a savepoint; log and swallow any error so the recorded edge itself never fails."""

    @functools.wraps(fn)
    def wrapper(session: Session, *args: Any, **kwargs: Any) -> Any:
        # The caller's own pending writes flush OUTSIDE the guard: their failure is the caller's to see, never
        # reported (and swallowed) as the ledger's.
        session.flush()
        try:
            with session.begin_nested():
                return fn(session, *args, **kwargs)
        except Exception:  # noqa: BLE001 — the ledger never fails the edge it records; the tick reconciles.
            log.exception("outcome-ledger: %s failed; the edge it records still lands", fn.__name__)
            return None

    return wrapper  # type: ignore[return-value]


def _task(session: Session, task: TaskRef) -> Optional[Task]:
    if isinstance(task, Task):
        return task
    # The seams write the task with Core UPDATEs the identity map never sees: read the row as it stands now.
    session.flush()
    return session.get(Task, int(task), populate_existing=True)


def _rows(session: Session, task_id: int) -> list[TaskOutcome]:
    return list(session.scalars(
        select(TaskOutcome).where(TaskOutcome.task_id == int(task_id)).order_by(TaskOutcome.dispatch_seq)
    ))


def _latest(session: Session, task_id: int) -> Optional[TaskOutcome]:
    """The task's newest non-excluded dispatch row."""
    return session.scalars(
        select(TaskOutcome)
        .where(TaskOutcome.task_id == int(task_id), TaskOutcome.excluded.is_(False))
        .order_by(TaskOutcome.dispatch_seq.desc()).limit(1)
    ).first()


def _open(session: Session, task_id: int) -> Optional[TaskOutcome]:
    """The task's dispatch row whose attempt has not ended yet."""
    row = _latest(session, task_id)
    return row if row is not None and row.ended_at is None else None


def _move(row: TaskOutcome, edge: str, target: str, now: datetime) -> None:
    states.check(edge, row.outcome_state, target)
    if row.outcome_state != target:
        row.outcome_state = target
        row.state_changed_at = now


# ── Dispatch ──────────────────────────────────────────────────────────────────────────────────────────


def _ready_at(session: Session, task_id: int) -> Optional[datetime]:
    """The task's latest `→ ready` transition after its last reset, or None."""
    from app.models.audit import ActivityEvent

    hidden_before = session.scalar(select(Task.artifacts_hidden_before).where(Task.id == int(task_id)))
    query = select(func.max(ActivityEvent.created_at)).where(
        ActivityEvent.kind.like("task:%"), ActivityEvent.data["id"].astext == str(task_id),
        ActivityEvent.data["state"].astext == "ready",
    )
    if hidden_before is not None:
        query = query.where(ActivityEvent.created_at >= hidden_before)
    return session.scalar(query)


def _hosting(node_id: Optional[UUID]) -> Optional[str]:
    if node_id is None:
        return None
    from app import node_allocation

    return "jaah_cloud" if node_id in node_allocation.jaah_cloud([node_id]) else "self_hosted"


@_ledger_write
def open_row(session: Session, task: Any, *, account_id: Optional[UUID], node_id: Any) -> Optional[TaskOutcome]:
    """Open this dispatch's row with its attribution snapshot — called where the dispatch's model (or SDK agent)
    is stamped. A system persona gets no row. A row the task left open (an attempt no edge closed) ends
    `abandoned`, and a coder row left `pending` at a lane enqueue ends `failed`: this dispatch replaces both."""
    task = _task(session, task)
    if task is None or task.persona not in LEDGER_PERSONAS:
        return None
    now = _now()
    for prior in _rows(session, task.id):
        if prior.excluded:
            continue
        if prior.ended_at is None:
            _end(session, prior, task, END_ABANDONED, reason="redispatched", now=now)
        elif prior.outcome_state == states.PENDING:
            # Settled at a lane enqueue, never merged, and dispatched again: the lane refused that attempt.
            _move(prior, states.EDGE_FAIL, states.FAILED, now)
            prior.end_kind, prior.end_reason = END_FAILED, "redispatched"
    seq = (session.scalar(select(func.max(TaskOutcome.dispatch_seq)).where(TaskOutcome.task_id == task.id)) or 0) + 1
    node_uuid = UUID(str(node_id)) if node_id else None
    account = session.get(Account, account_id) if account_id is not None else None
    node = session.get(Node, node_uuid) if node_uuid is not None else None
    sdk = task.run_agent_id is not None
    row = TaskOutcome(
        org_id=task.org_id, task_id=task.id, task_title=(task.title or "")[:200], dispatch_seq=seq,
        project_id=task.project_id, persona=task.persona,
        agent_kind="sdk" if sdk else "builtin",
        provider=None if sdk else ((account.provider if account is not None else task.bound_provider) or None),
        agent_id=task.run_agent_id, agent_commit=task.run_agent_commit if sdk else None,
        model=task.dispatched_model, model_family=dimensions.model_family(task.dispatched_model),
        auth_kind=account.auth_kind if account is not None else None,
        account_id=account.id if account is not None else None,
        node_id=node_uuid, node_hosting=_hosting(node_uuid),
        node_isolation=("microvm" if node.microvm else "static") if node is not None else None,
        task_ready_at=_ready_at(session, task.id), dispatched_at=now, state_changed_at=now,
        shadow=bool(getattr(task, "shadow", False)),
        clarity_score=dimensions.clarity(task.clarity_score),
    )
    session.add(row)
    session.flush()
    return row


@_ledger_write
def link_routing_decision(session: Session, task_id: int, decision_id: UUID) -> Optional[TaskOutcome]:
    """Name the routing decision the claim finalized on the dispatch row the lease opened (the task's open row), so
    the decision is graded by this row's outcome. A row already linked keeps its decision."""
    row = _open(session, task_id)
    if row is None or row.routing_decision_id is not None:
        return None
    row.routing_decision_id = decision_id
    return row


@_ledger_write
def attach_session(session: Session, task_id: int, agent_session_id: str, started_at: Optional[datetime]) -> None:
    """Link the open row to the session the launch announced (`agent_sessions.agent_session_id`, the runner's
    own id) and stamp `first_active_at`."""
    row = _open(session, task_id)
    if row is None:
        return
    found = session.scalars(
        select(AgentSession).where(AgentSession.agent_session_id == str(agent_session_id))
    ).first()
    if found is not None:
        # A resume that announces the same session id: its usage entry is cumulative, so only the newest dispatch
        # prices it (two rows pricing one entry would double the task's cost).
        for earlier in _rows(session, task_id):
            if earlier.id != row.id and earlier.agent_session_id == found.id:
                earlier.agent_session_id = None
                earlier.cost_usd, earlier.cost_status = None, "partial"
        row.agent_session_id = found.id
        started_at = started_at or found.started_at
    if row.first_active_at is None:
        row.first_active_at = started_at or _now()


# ── Attempt end ───────────────────────────────────────────────────────────────────────────────────────


def _price(session: Session, task: Task, row: TaskOutcome) -> None:
    """This dispatch's tokens and dollars: its own session's entry in `Task.metrics["sessions"]` only, never the
    task's totals (which would multiply cost by the dispatch count), and its node minutes."""
    if row.agent_session_id is None:
        row.cost_status = row.cost_status or "not_captured"
        return
    agent = session.get(AgentSession, row.agent_session_id)
    if agent is None:
        return
    if agent.started_at is not None:
        end = agent.ended_at or row.ended_at or _now()
        row.node_minutes = Decimal(max((end - agent.started_at).total_seconds(), 0) / 60).quantize(Decimal("0.01"))
    sessions = (task.metrics or {}).get("sessions") or {}
    entry = sessions.get(agent.agent_session_id) if isinstance(sessions, dict) else None
    if not isinstance(entry, dict):
        row.cost_status = "not_captured"
        return
    from app.task_run_metrics import live_rates, price_fold, scope_cost

    row.tokens_in = int(entry.get("tokens_in") or 0)
    row.tokens_out = int(entry.get("tokens_out") or 0)
    one = SimpleNamespace(id=task.id, persona=task.persona, metrics={"sessions": {agent.agent_session_id: entry}})
    usd, status = price_fold(scope_cost([one], live_rates(session, [one])), False, task.id)
    row.cost_usd = Decimal(usd) if usd is not None else None
    row.cost_status = _COST_STATUS.get(status, "not_captured")


def _end(session: Session, row: TaskOutcome, task: Optional[Task], end_kind: str, *, reason: Optional[str],
         now: datetime, awaits_merge: bool = False) -> None:
    if end_kind == END_FAILED:
        _move(row, states.EDGE_FAIL, states.FAILED, now)
    elif end_kind == END_ABANDONED:
        _move(row, states.EDGE_ABANDON, states.ABANDONED, now)
    elif not awaits_merge:
        # No merge to wait for (an analysis or `outcome` settle, a `no_pr` close, an operator's Complete): nothing to
        # survive. A `merged_pr` settle awaits its merge: `on_merged` moves it now (self-merge) or when the lane
        # merges it (settled at QUEUED).
        _move(row, states.EDGE_SETTLE, states.SETTLED, now)
    row.ended_at, row.end_kind, row.end_reason = now, end_kind, reason
    if task is not None:
        _price(session, task, row)


@_ledger_write
def end_row(session: Session, task: Any, *, end_kind: str, review_tier: Any = None,
            reason: Optional[str] = None, awaits_merge: bool = False) -> Optional[TaskOutcome]:
    """End the open dispatch row: `settled` (every DONE writer; `awaits_merge` for a `merged_pr` settle), `failed`
    (the runner's own `task_fail`) or `abandoned` (every other attempt end). `review_tier` is the one
    runner-declared fact, normalised and never refused; the settle's `findings` land through `add_findings`.

    A settle with no open row is a recovery — a task settled from `system_failed` after its last attempt ended the
    row `abandoned`/`failed` — and reopens that row (the `recover` edge), or one the lane already merged, which only
    takes the declared `review_tier`."""
    task = _task(session, task)
    if task is None:
        return None
    now = _now()
    row = _open(session, task.id)
    if row is None and end_kind == END_SETTLED:
        row = _latest(session, task.id)
        if row is None:
            return None
        if row.outcome_state in (states.ABANDONED, states.FAILED) and row.end_reason != "redispatched":
            _move(row, states.EDGE_RECOVER, states.PENDING, now)
        elif row.end_kind == END_SETTLED and row.review_tier == dimensions.REVIEW_TIER_UNKNOWN:
            if review_tier is not None:
                row.review_tier = dimensions.normalise_review_tier(review_tier)
            return row
        else:
            return None
    if row is None:
        return None
    if review_tier is not None:
        row.review_tier = dimensions.normalise_review_tier(review_tier)
    _end(session, row, task, end_kind, reason=reason, now=now, awaits_merge=awaits_merge)
    return row


@_ledger_write
def add_findings(session: Session, task_id: int, by_detector: dict[str, int], *, malformed: int = 0,
                 outcome_id: Optional[UUID] = None) -> Optional[TaskOutcome]:
    """Count security findings (`review_findings` rows, already written) against one dispatch row: `outcome_id`'s (a
    CI scan's origin), else the task's newest (the settle that reported them). `security_by_detector` takes each
    lane's count and `security_findings` stays its sum; `malformed` counts the settle's dropped entries. Attribution
    only: the row's state never moves. Without `outcome_id` the row is the newest ENDED one: a failed attempt's retry
    may already have opened the next dispatch's row, which reported nothing."""
    where = (TaskOutcome.id == outcome_id) if outcome_id is not None else TaskOutcome.ended_at.is_not(None)
    row = session.scalars(
        select(TaskOutcome).where(where, TaskOutcome.task_id == int(task_id), TaskOutcome.excluded.is_(False))
        .order_by(TaskOutcome.dispatch_seq.desc()).limit(1)
    ).first()
    if row is None:
        return None
    counts = {str(k): int(v) for k, v in (row.security_by_detector or {}).items()}
    for lane, n in by_detector.items():
        if n > 0:
            counts[lane] = counts.get(lane, 0) + int(n)
    if counts or row.security_findings is not None:
        row.security_by_detector = counts
        row.security_findings = sum(counts.values())
    if malformed > 0:
        row.findings_malformed = (row.findings_malformed or 0) + int(malformed)
    return row


# ── Merge, heads, commits ─────────────────────────────────────────────────────────────────────────────


def _complexity(session: Session, task: Task, row: TaskOutcome) -> None:
    stats = session.get(TaskPrStats, task.id)
    if stats is not None:
        row.additions, row.deletions, row.files_changed = stats.additions, stats.deletions, stats.files_changed
    paths: list[str] = []
    for changes in session.scalars(select(TaskPrChanges).where(TaskPrChanges.task_id == task.id)):
        paths.extend(str(p) for p in (getattr(changes, "files", None) or []))
    row.language = dimensions.language_of(paths) or row.language
    size = None if row.additions is None or row.deletions is None else row.additions + row.deletions
    row.complexity_bucket = dimensions.complexity_bucket(size, row.files_changed, row.clarity_score)


def survival_days(session: Session, org_id: Any) -> int:
    """The org's survival window in days (`plan_terms.outcome_survival_days`, the org override over the plan)."""
    from app import plan_terms
    from app.models.orgs import Org

    org = session.get(Org, org_id)
    return int(plan_terms.outcome_survival_days(session, org)) if org is not None else 14


def merge_sha_of(session: Session, row: TaskOutcome) -> Optional[str]:
    """The origin's merge commit: the row's (a lane merge), else the PR provenance its settle captured. Without it a
    revert of the merge commit (git's `This reverts commit <merge>`) matches nothing."""
    if row.merge_sha:
        return row.merge_sha
    from app.outcome_ledger.scan import pr_number_of  # scan imports this module

    number = pr_number_of(row.pr_url)
    changes = session.get(TaskPrChanges, (row.task_id, number)) if number is not None and row.task_id else None
    return changes.merge_sha if changes is not None and changes.merge_sha else None


@_ledger_write
def on_merged(session: Session, task: Any, *, pr_url: Optional[str], merge_sha: Optional[str],
              merged_at: Optional[datetime], lane_minutes: Optional[float] = None) -> Optional[TaskOutcome]:
    """The dispatch's PR merged (self-merge attestation or the lane's `merged` event): `pending → merged`, the
    survival horizon fixed now, and the gate facts the heads hold folded onto the row."""
    task = _task(session, task)
    if task is None:
        return None
    row = _latest(session, task.id)
    # The `pr.merged` webhook's source, whether or not the ledger holds a pending row; once per task and PR.
    from app.outcome_ledger.scan import pr_number_of
    from app.webhooks.engine import record_pr_event

    merged_url = pr_url or (row.pr_url if row is not None else None)
    record_pr_event(session, task, "pr:merged", pr_url=merged_url,
                    pr_number=pr_number_of(merged_url) if merged_url else None,
                    merge_sha=merge_sha or (row.merge_sha if row is not None else None))
    if row is None or row.outcome_state != states.PENDING:
        return row
    now = _now()
    merged_at = merged_at or now
    _move(row, states.EDGE_MERGE, states.MERGED, now)
    row.merged_at, row.merge_sha, row.pr_url = merged_at, merge_sha or row.merge_sha, pr_url or row.pr_url
    # A self-merge or a reconciled merge names no sha; the settle's provenance (stored before it) does.
    row.merge_sha = row.merge_sha or merge_sha_of(session, row)
    row.survival_ends_at = merged_at + timedelta(days=survival_days(session, row.org_id))
    if row.ended_at is None:
        row.ended_at, row.end_kind = now, END_SETTLED
        _price(session, task, row)
    heads = list(session.scalars(select(TaskOutcomeHead).where(TaskOutcomeHead.outcome_id == row.id)))
    row.pushes_after_enqueue = sum(1 for head in heads if head.after_enqueue)
    row.lane_failures = sum(1 for head in heads if head.verdict == "fail")
    row.pr_opened_at = session.scalar(select(func.min(TaskPr.opened_at)).where(TaskPr.task_id == task.id))
    row.enqueued_at = session.scalar(select(func.min(MergeLaneRow.created_at)).where(MergeLaneRow.task_id == task.id))
    if lane_minutes is not None:
        row.lane_minutes = Decimal(str(round(lane_minutes, 2)))
    _complexity(session, task, row)
    _recompute(session, row, now)
    if row.merge_sha and row.project_id is not None:
        # The change is on the integration stage (a runner's self-merge; the lane's own `merged` event lands it
        # too, the same arrival): its deploy and verify after arrival.
        from app.releases import arrivals  # noqa: PLC0415 — the releases package reads this ledger

        arrivals.land_integration(session, row.project_id, row.merge_sha, merged_at=row.merged_at)
    return row


_SHA = re.compile(r"\A[0-9a-f]{7,64}\Z")


def _shas(values: Iterable[Any]) -> list[str]:
    """The well-formed (7–64 hex) shas among `values`, lower-cased; anything else is dropped, never refused."""
    out = [v.strip().lower() for v in values if isinstance(v, str)]
    return [v for v in out if _SHA.match(v)]


def _short_match(sha: str, known: Iterable[str]) -> bool:
    """Whether `sha` and one of `known` name the same commit (either may be abbreviated, both ≥ 7 hex)."""
    sha = sha.lower()
    return any(k.startswith(sha) or sha.startswith(k) for k in known)


@_ledger_write
def add_head(session: Session, task_id: int, head_sha: str, *, pushed_shas: list[str], source: str = "runner_push",
             pushed_at: Optional[datetime] = None, ref: Optional[str] = None) -> Optional[TaskOutcomeHead]:
    """Record one push for the task's current dispatch. `after_enqueue` is whether the PR was in the lane by then
    (any lane row of the task not closed)."""
    row = _latest(session, task_id)
    if row is None:
        return None
    sha = head_sha.strip().lower()
    pushed = _shas(pushed_shas)[:MAX_PUSHED_SHAS]
    head = session.scalars(
        select(TaskOutcomeHead).where(TaskOutcomeHead.task_id == int(task_id), TaskOutcomeHead.head_sha == sha)
    ).first()
    if head is not None:
        # The same head pushed again by a later dispatch is that dispatch's push.
        head.outcome_id = row.id
        head.pushed_shas = sorted(set(head.pushed_shas) | set(pushed))[:MAX_PUSHED_SHAS]
        return head
    # Enqueued once means enqueued: a fix pushed after the lane failed the PR is still a push after enqueue.
    enqueued = session.scalar(
        select(func.count()).select_from(MergeLaneRow)
        .where(MergeLaneRow.task_id == int(task_id), MergeLaneRow.state != "closed")
    )
    head = TaskOutcomeHead(
        org_id=row.org_id, outcome_id=row.id, task_id=int(task_id), head_sha=sha, ref=ref, pushed_shas=pushed,
        pushed_at=pushed_at or _now(), source=source, after_enqueue=bool(enqueued), verdict="pending",
    )
    session.add(head)
    session.flush()
    return head


@_ledger_write
def set_head_verdict(session: Session, task_id: int, head_sha: str, verdict: str, source: str) -> None:
    """A gate's verdict on one pushed head (`lane`: the merge lane's failed/merged/superseded; `forge_ci`: the
    scan). A head the daemon never reported is recorded here, with no pushed commits."""
    row = _latest(session, task_id)
    if row is None:
        return
    sha = head_sha.strip().lower()
    head = session.scalars(
        select(TaskOutcomeHead).where(TaskOutcomeHead.task_id == int(task_id), TaskOutcomeHead.head_sha == sha)
    ).first()
    if head is None:
        head = TaskOutcomeHead(org_id=row.org_id, outcome_id=row.id, task_id=int(task_id), head_sha=sha,
                               pushed_shas=[], source="lane_verdict", after_enqueue=False)
        session.add(head)
    head.verdict, head.verdict_source, head.verdict_at = verdict, source, _now()
    session.flush()
    if row.outcome_state in states.LANDED_STATES:
        row.lane_failures = session.scalar(
            select(func.count()).select_from(TaskOutcomeHead)
            .where(TaskOutcomeHead.outcome_id == row.id, TaskOutcomeHead.verdict == "fail")
        )


@_ledger_write
def set_commit_facts(session: Session, task: Any, *, pr_number: Optional[int], pr_commits: list[Any]) -> None:
    """The PR's commit list arrived (after the merge): `nonrunner_commits` = PR commits no runner push sent.
    NULL — unknown — when no push of this dispatch was ever reported."""
    del pr_number
    task = _task(session, task)
    if task is None:
        return
    row = _latest(session, task.id)
    if row is None:
        return
    heads = list(session.scalars(select(TaskOutcomeHead).where(TaskOutcomeHead.outcome_id == row.id)))
    # The runner's own pushes, plus the lane's conflict merges (never a non-runner fix); a head known only from a
    # gate verdict proves nothing about who wrote it.
    runner = {s.lower() for head in heads if head.source in ("runner_push", "lane_resolve")
              for s in [*head.pushed_shas, head.head_sha]}
    if not any(head.source == "runner_push" and head.pushed_shas for head in heads):
        return
    shas = _shas(c.get("sha") if isinstance(c, dict) else c for c in pr_commits)
    row.nonrunner_commits = sum(1 for sha in shas if not _short_match(sha, runner))


# ── Operator edges ────────────────────────────────────────────────────────────────────────────────────


@_ledger_write
def exclude(session: Session, task_id: int, reason: str) -> int:
    """Take the task's rows out of every metric, state frozen. `reset` excludes only rows not yet judged-merged
    (an earlier dispatch that merged stays a real outcome); `deleted`/`moved`/`cancelled` exclude all."""
    now = _now()
    count = 0
    for row in _rows(session, task_id):
        if row.excluded:
            continue
        if reason == EXCLUDE_RESET and row.outcome_state in states.LANDED_STATES | states.FINAL_STATES:
            continue
        row.excluded, row.excluded_reason = True, reason
        if row.ended_at is None:
            row.ended_at = now
        count += 1
    return count


# ── Links and judging ─────────────────────────────────────────────────────────────────────────────────


def _recompute(session: Session, row: TaskOutcome, now: datetime) -> None:
    """A landed row's state from its LIVE links (Architecture §4): the worst defect, else `survived` once the
    window is over, else `merged`."""
    if row.outcome_state not in states.LANDED_STATES:
        return
    live = [
        link.defect_state for link in session.scalars(
            select(OutcomeLink).where(OutcomeLink.origin_outcome_id == row.id)
        ) if states.is_live(link.state, link.confidence)
    ]
    target = states.worst(live)
    if target is None:
        over = row.survival_ends_at is not None and now >= row.survival_ends_at
        target = states.SURVIVED if over else (states.DEPLOYED if row.deployed_at is not None else states.MERGED)
    if target == row.outcome_state:
        return
    edge = states.EDGE_SURVIVE if (target == states.SURVIVED and row.outcome_state in
                                   (states.MERGED, states.DEPLOYED)) else states.EDGE_LINK
    _move(row, edge, target, now)


@_ledger_write
def apply_link(session: Session, link: OutcomeLink) -> Optional[str]:
    """A link was proposed, confirmed or dismissed: recompute its origin's state from all its links, then the
    epic's analysis rows that inherit it. Returns the origin's new state."""
    return _apply(session, link)


def _apply(session: Session, link: OutcomeLink) -> Optional[str]:
    """`apply_link`'s body, for callers already inside a ledger write (no second savepoint)."""
    session.flush()
    row = session.get(TaskOutcome, link.origin_outcome_id)
    if row is None or row.excluded:
        return None
    now = _now()
    _recompute(session, row, now)
    task = session.get(Task, row.task_id) if row.task_id is not None else None
    if task is not None and task.parent_task_id is not None:
        inherit_epic(session, task.parent_task_id, now)
    return row.outcome_state


@_ledger_write
def propose_link(session: Session, origin: TaskOutcome, *, kind: str, evidence_kind: str, evidence_ref: str,
                 evidence_title: Optional[str], confidence: Decimal, defect_state: str) -> Optional[OutcomeLink]:
    """A detector's link: inserted `proposed` once per (origin, kind, evidence_ref), then the origin re-judged.
    Idempotent — a re-run finds the row and changes nothing, so a dismissed link is never re-proposed. A stronger
    confidence for the same evidence (a later page saw the closing keyword) raises a still-proposed link's
    confidence; it never lowers one. Returns the link, new or existing."""
    link = session.scalars(select(OutcomeLink).where(
        OutcomeLink.origin_outcome_id == origin.id, OutcomeLink.kind == kind, OutcomeLink.evidence_ref == evidence_ref,
    )).first()
    if link is not None:
        if link.state == "proposed" and confidence > link.confidence:
            link.confidence = confidence
            _apply(session, link)
        return link
    link = OutcomeLink(
        org_id=origin.org_id, origin_outcome_id=origin.id, kind=kind, evidence_kind=evidence_kind,
        evidence_ref=evidence_ref[:500], evidence_title=(evidence_title or "")[:200] or None, confidence=confidence,
        defect_state=defect_state, state="proposed",
    )
    session.add(link)
    _apply(session, link)
    return link


def decide_link(session: Session, link: OutcomeLink, *, state: str, actor_id: Any) -> Optional[str]:
    """An Owner/Admin confirmed or dismissed `link`: recorded with who and when, the origin re-judged from the links
    left (a dismissal returns it to `merged`, or `survived` past its window). A confirm sets confidence to 1.0.
    Not a swallowed ledger write: the operator's own action must fail loudly. Returns the origin's new state."""
    if state not in ("confirmed", "dismissed"):
        raise ValueError(f"cannot decide a link {state!r}")
    now = _now()
    link.state, link.decided_by_id, link.decided_at, link.updated_by_id = state, actor_id, now, actor_id
    if state == "confirmed":
        link.confidence = Decimal("1.00")
    return _apply(session, link)


def add_caused_by(session: Session, origin: TaskOutcome, *, evidence_task: Task, actor_id: Any) -> OutcomeLink:
    """An operator's explicit "caused by": `evidence_task` fixes a defect `origin` caused. Created `confirmed` at
    1.0 (an existing link for the same pair is confirmed in place, a dismissed one included: the operator said so
    now). Raises on failure, like `decide_link`."""
    ref = str(evidence_task.id)
    link = session.scalars(select(OutcomeLink).where(
        OutcomeLink.origin_outcome_id == origin.id, OutcomeLink.kind == "caused_by", OutcomeLink.evidence_ref == ref,
    )).first()
    if link is None:
        link = OutcomeLink(
            org_id=origin.org_id, origin_outcome_id=origin.id, kind="caused_by", evidence_kind="task",
            evidence_ref=ref, evidence_title=(evidence_task.title or "")[:200] or None, confidence=Decimal("1.00"),
            defect_state="fixed_forward", state="confirmed", created_by_id=actor_id,
        )
        session.add(link)
    decide_link(session, link, state="confirmed", actor_id=actor_id)
    return link


# ── Production QA facts (`production_qa_edges`, applied by `production_qa.ledger`) ─────────────────────────

#: What a production fact did to the ledger: `applied` (written, or nothing of the task's to write it on — a task
#: with no row, or only rows that never merged) or `deferred` (the task's newest dispatch has not settled its merge
#: yet: the fact waits for it). A swallowed error returns None, so the applier retries the fact on its next pass.
FACT_APPLIED = "applied"
FACT_DEFERRED = "deferred"
#: The `outcome_links` kind and evidence of an incident link: the incident is a console record like a task, so its
#: evidence is `task`-kind with an `incident:<id>` ref (the console links it to the incident page).
INCIDENT_LINK_KIND = "incident"
INCIDENT_EVIDENCE_KIND = "task"
#: A production-QA link that is not an operator's confirmation counts as a `high` suspect: LIVE.
INCIDENT_HIGH_CONFIDENCE = Decimal("0.90")


def incident_ref(incident_id: int) -> str:
    return f"incident:{int(incident_id)}"


def _fact_row(session: Session, task_id: int) -> Union[TaskOutcome, str]:
    """The task's newest landed (merged) row a production fact lands on, else `FACT_DEFERRED` while its newest
    dispatch may still merge, else `FACT_APPLIED` (nothing to write it on)."""
    landed = session.scalars(
        select(TaskOutcome)
        .where(TaskOutcome.task_id == int(task_id), TaskOutcome.excluded.is_(False),
               TaskOutcome.outcome_state.in_(states.LANDED_STATES))
        .order_by(TaskOutcome.dispatch_seq.desc()).limit(1)
    ).first()
    if landed is not None:
        return landed
    latest = _latest(session, task_id)
    if latest is not None:
        return FACT_DEFERRED if latest.outcome_state == states.PENDING else FACT_APPLIED
    return FACT_APPLIED  # No row at all, or every row excluded (an operator took the task out).


@_ledger_write
def record_deployed(session: Session, task_id: int, deployed_at: datetime) -> Optional[str]:
    """The task's PR shipped to production at `deployed_at`: the row keeps its first production deploy and moves
    `merged → deployed`. The survival window stays the one fixed at merge (12243 decides `survived`)."""
    row = _fact_row(session, task_id)
    if isinstance(row, str):
        return row
    if row.deployed_at is None or deployed_at < row.deployed_at:
        row.deployed_at = deployed_at
    if row.outcome_state == states.MERGED:
        _move(row, states.EDGE_DEPLOY, states.DEPLOYED, _now())
    return FACT_APPLIED


@_ledger_write
def withdraw_deployed(session: Session, task_id: int, still_deployed_at: Optional[datetime]) -> Optional[str]:
    """A `deployed` fact was revoked: every row of the task that carries a deploy keeps the earliest one still
    standing (`still_deployed_at`), or none, and is re-judged (`deployed → merged`, or `survived` past its window)."""
    now = _now()
    for row in session.scalars(select(TaskOutcome).where(
        TaskOutcome.task_id == int(task_id), TaskOutcome.excluded.is_(False), TaskOutcome.deployed_at.is_not(None),
    )):
        row.deployed_at = still_deployed_at
        _recompute(session, row, now)
    return FACT_APPLIED


def _incident_link(session: Session, task_id: int, incident_id: int) -> Optional[OutcomeLink]:
    """The incident's link on any of the task's rows (a later dispatch may have landed since it was written)."""
    return session.scalars(
        select(OutcomeLink).join(TaskOutcome, TaskOutcome.id == OutcomeLink.origin_outcome_id)
        .where(TaskOutcome.task_id == int(task_id), TaskOutcome.excluded.is_(False),
               OutcomeLink.kind == INCIDENT_LINK_KIND,
               OutcomeLink.evidence_ref == incident_ref(incident_id))
        .order_by(OutcomeLink.created_at.desc()).limit(1)
    ).first()


@_ledger_write
def record_incident_link(session: Session, task_id: int, incident_id: int, *, title: Optional[str],
                         confirmed: bool) -> Optional[str]:
    """Production QA links the task's PR to an incident: an `incident` link (`incident_linked`), `confirmed` when an
    operator confirmed the suspect, else proposed at `high` confidence — LIVE either way — and the origin re-judged.
    A link this applier dismissed comes back; one an operator dismissed in the ledger stays dismissed."""
    row = _fact_row(session, task_id)
    if isinstance(row, str):
        return row
    state = "confirmed" if confirmed else "proposed"
    confidence = Decimal("1.00") if confirmed else INCIDENT_HIGH_CONFIDENCE
    link = _incident_link(session, task_id, incident_id)
    if link is None:
        link = OutcomeLink(
            org_id=row.org_id, origin_outcome_id=row.id, kind=INCIDENT_LINK_KIND,
            evidence_kind=INCIDENT_EVIDENCE_KIND, evidence_ref=incident_ref(incident_id),
            evidence_title=(title or "")[:200] or None, confidence=confidence, defect_state=states.INCIDENT_LINKED,
            state=state,
        )
        session.add(link)
    elif link.state == "dismissed" and link.decided_by_id is not None:
        return FACT_APPLIED  # The operator's own dismissal in the ledger wins.
    elif link.state == "dismissed" or (confirmed and link.state != "confirmed"):
        link.state, link.confidence, link.decided_at = state, confidence, None
    elif link.state == "proposed":
        link.confidence = max(link.confidence, confidence)
    _apply(session, link)
    return FACT_APPLIED


@_ledger_write
def withdraw_incident_link(session: Session, task_id: int, incident_id: int) -> Optional[str]:
    """Production QA dropped the suspect: its link is dismissed (by no one: the system) and the origin re-judged
    from the links left. An operator's own decision on the link in the ledger stands."""
    link = _incident_link(session, task_id, incident_id)
    if link is not None and link.state != "dismissed" and link.decided_by_id is None:
        link.state, link.decided_by_id, link.decided_at = "dismissed", None, _now()
        _apply(session, link)
    return FACT_APPLIED


@_ledger_write
def record_fix(session: Session, task_id: int, incident_id: int) -> Optional[str]:
    """The task's deploy resolved an incident (`resolved_by_fix`). A fact only (R3): the fixer's row is not moved —
    its survival window still decides `survived` — so this waits for the fixer's merge and records nothing on the
    row; the applied fact is read from `production_qa_edges`."""
    del incident_id
    row = _fact_row(session, task_id)
    return row if isinstance(row, str) else FACT_APPLIED


def _final_child_rows(session: Session, epic_id: int) -> Optional[list[TaskOutcome]]:
    """Each build child's newest row, or None while any build child is not yet judged."""
    children = list(session.scalars(
        select(Task.id).where(Task.parent_task_id == epic_id, Task.persona.in_(BUILD_PERSONAS))
    ))
    out: list[TaskOutcome] = []
    for child in children:
        row = _latest(session, child)
        if row is None:
            if any(not r.excluded for r in _rows(session, child)) or not _rows(session, child):
                return None  # Not dispatched yet, or still running.
            continue  # Every row excluded: an operator took this child out.
        if row.outcome_state not in states.FINAL_STATES:
            return None
        out.append(row)
    return out


def inherit_epic(session: Session, epic_id: int, now: datetime) -> int:
    """D5: each analysis row under `epic_id` takes its build siblings' result once all are final — `survived`,
    the worst defect, or `failed` when none merged. An epic with no build child keeps its rows `settled`."""
    analysis = list(session.scalars(
        select(TaskOutcome).join(Task, Task.id == TaskOutcome.task_id).where(
            Task.parent_task_id == epic_id, TaskOutcome.persona.not_in(BUILD_PERSONAS),
            TaskOutcome.excluded.is_(False),
            TaskOutcome.outcome_state.in_((states.SETTLED, states.SURVIVED, states.FAILED, *states.DEFECT_STATES)),
        )
    ))
    if not analysis:
        return 0
    children = _final_child_rows(session, epic_id)
    if not children:
        return 0  # Not all final yet, or no build child at all: stays as it is.
    landed = [row for row in children if row.outcome_state in states.LANDED_STATES]
    if not landed:
        target = states.FAILED
    else:
        target = states.worst([row.outcome_state for row in landed]) or states.SURVIVED
    moved = 0
    for row in analysis:
        if row.outcome_state != target:
            _move(row, states.EDGE_INHERIT, target, now)
            moved += 1
    return moved
