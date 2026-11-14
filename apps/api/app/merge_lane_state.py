"""What every edition reads of the merge lane: whether a project is lane-active, which tasks are lane runs, which
done tasks still wait on a lane row, and the one hook a task leaving `active` calls.

The lane's engine — its queue moves, scheduler, verbs, console and alerts — is `app/merge_lane.py`, which only an
edition with the edition map's `merge_lane` row ships; it re-exports these names. Here, in an edition without the
row, no project is lane-active (`lane_states`), no lane run is ever filed, and `merge_lane_rows` stays empty, so
every read below answers "none".
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app import edition_features, plan_terms, secret_redact
from app.clock import utcnow
from app.models.merge_lane import MergeLaneCertificate, MergeLaneRow
from app.models.orgs import Org
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.task_lifecycle import ACTIVE, DONE, SYSTEM_FAILED
from app.pipeline_gates.full_gate import current_hash, has_full_gate_sql

#: The persona every lane task carries (`persona_registry`'s merge-lane persona).
LANE_PERSONA = "merge_lane"
#: The bounds of a project's `merge_lane_batch_max` (`merge_lane.batch_max`).
LANE_BATCH_MAX_BOUNDS = (1, 50)
#: The forges whose `pr merge` can pin a head SHA atomically (L3; Bitbucket cannot).
LANE_FORGES = ("github", "gitlab", "azure_devops")

QUEUED, TESTING, MERGED, FAILED, SUPERSEDED, CLOSED = (
    "queued", "testing", "merged", "failed", "superseded", "closed",
)
#: The states holding the one-open-row-per-(project, PR) index (`uq_merge_lane_open`).
OPEN_STATES = (QUEUED, TESTING)

#: `merge_lane_rows.note`: the longest line kept.
_NOTE_MAX = 300


def is_lane_persona(persona_name: Optional[str]) -> bool:
    """Whether a task of this persona is a merge-lane run."""
    return persona_name == LANE_PERSONA


def _note_line(text: str) -> str:
    """Text as a `note`-sized line (`secret_redact.one_line` at `_NOTE_MAX`)."""
    return secret_redact.one_line(text, _NOTE_MAX)


#: How the merge-lane toolbox opens its `task.fail` reason (`merge lane stopped: FenceError: …`): redundant on a
#: run row, as the exception's class name is to an operator.
_RUN_FAILURE_LEAD = re.compile(r"^merge lane stopped: (?:[A-Z]\w*(?:Error|Exception): )?")


def run_failure_line(reason: str) -> str:
    """A runner's `task.fail` reason as its termination note keeps it (and a lane run's row shows it): a
    `_note_line` without the lane's own lead (`merge lane stopped: FenceError: the gate's sandbox would not
    start: …` -> `the gate's sandbox would not start: …`); any other reason has no such lead."""
    return _RUN_FAILURE_LEAD.sub("", _note_line(reason))


# ── Eligibility (L3) ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaneState:
    """Whether a project is lane-active, and why not."""

    active: bool
    reason: Optional[str]
    offered: bool = False


def _ineligible_reason(project: Project) -> Optional[str]:
    """The first L3 condition this project fails, as one line; None when it passes them all."""
    if not project.push_enabled:
        return "Push is off for this project, so the lane cannot merge."
    if not project.has_repo or not (project.repo_url or "").strip():
        return "This project has no repository."
    if project.forge not in LANE_FORGES:
        if project.forge == "bitbucket":
            return "Bitbucket cannot merge a pinned commit, so the lane is not available."
        return "The lane needs a GitHub, GitLab or Azure DevOps repository."
    return None


def lane_states(session: Session, projects: Iterable[Project]) -> dict[int, LaneState]:
    """`lane_state` for many projects in a fixed number of queries (the project list's projection).

    The org flag is read once per org through `plan_terms.merge_lane_enabled`, never off the columns; then the
    project's own switch (`projects.merge_lane_enabled`), then L3. An edition
    without the lane (it ships no sandboxed gate to run PR code under) refuses it whatever the flag says, so no
    runner settles on a queue nobody drains."""
    projects = list(projects)
    if not edition_features.enabled("merge_lane"):
        # Lazy: `ce_caps` imports `org_limits`, which reaches this module through `budgets`.
        from app.ce_caps import NOT_IN_CE

        return {project.id: LaneState(active=False, reason=NOT_IN_CE, offered=False) for project in projects}
    flags: dict[Any, bool] = {}
    out: dict[int, LaneState] = {}
    # A page's orgs in one read, held here (the identity map holds rows weakly), so each `get` below is a hit.
    org_ids = sorted({project.org_id for project in projects if project.org_id is not None})
    orgs = {org.id: org for org in session.scalars(select(Org).where(Org.id.in_(org_ids)))} if len(org_ids) > 1 else {}
    for project in projects:
        if project.org_id not in flags:
            org = orgs.get(project.org_id) or (
                session.get(Org, project.org_id) if project.org_id is not None else None
            )
            flags[project.org_id] = bool(org is not None and plan_terms.merge_lane_enabled(session, org))
        if not flags[project.org_id]:
            out[project.id] = LaneState(active=False, reason=None, offered=False)
            continue
        if not project.merge_lane_enabled:
            # The project's own switch, off by default: offered, not chosen. Open rows drain (`lane_running`).
            out[project.id] = LaneState(active=False, reason=None, offered=True)
            continue
        reason = _ineligible_reason(project)
        out[project.id] = LaneState(active=reason is None, reason=reason, offered=True)
    return out


def lane_state(session: Session, project: Project) -> LaneState:
    """Is `project` lane-active (L3)? Every reader — runner verb, console, lease filter, CI — asks here."""
    return lane_states(session, [project])[project.id]


def gating_node_ids(session: Session) -> set[str]:
    """Node uuid strings where an `active` lane run with a `testing` batch and a full gate holds a slot;
    placement pauses new runner dispatch there (`routes/fleet.py::fleet_allocation`)."""
    gating = select(MergeLaneRow.id).where(
        MergeLaneRow.lane_task_id == Task.id, MergeLaneRow.state == TESTING).exists()
    return {str(node) for node in session.scalars(
        select(Worktree.node_id).distinct()
        .join(Task, Task.id == Worktree.task_id)
        .join(Project, Project.id == Task.project_id)
        .where(
            # `slot_state.TASK` spelled out: keeps `slot_state`'s import graph out.
            Worktree.state == "task",
            Task.persona == LANE_PERSONA,
            Task.state == ACTIVE,
            has_full_gate_sql(Project),
            gating,
        )
    )}


def lane_task_left_active(session: Session, task_id: int) -> None:
    """The L10 hook, called by every writer that moves a task out of `active` (`merge_lane.lane_task_left_active`):
    nothing to do in an edition without the lane, which never files a lane run."""
    if not edition_features.enabled("merge_lane"):
        return
    from app import merge_lane

    merge_lane.lane_task_left_active(session, task_id)


# ── Done but not delivered ────────────────────────────────────────────────────────────────────────────


def awaiting_lane_merge_clause(task_id_column: Any) -> Any:
    """SQL predicate: the task's latest lane row is still open (`queued`/`testing`/`failed`). A runner settles
    `done` once its PR is queued, so such a task is done-but-not-delivered: no dependency gate or epic rollup may
    treat it as `done` yet. `merged`, `closed` and `superseded` rows, and a task with no row, do not hold it."""
    # "An open row with no newer row", not `id == (latest-row subquery)`: a scalar subquery two levels down does not
    # correlate to the outer task, and read the newest row of ANY task.
    held, newer = aliased(MergeLaneRow), aliased(MergeLaneRow)
    superseded_by = (select(newer.id).where(
        newer.task_id == held.task_id,
        (newer.created_at > held.created_at) | ((newer.created_at == held.created_at) & (newer.id > held.id)),
    ).exists())
    return (select(held.id)
            .where(held.task_id == task_id_column, held.state.in_((QUEUED, TESTING, FAILED)), ~superseded_by)
            .exists())


def awaiting_lane_merge(session: Session, task_ids: Iterable[int]) -> set[int]:
    """The subset of `task_ids` that is done-but-awaiting-the-lane (`awaiting_lane_merge_clause`), in one query.
    Every reader that asks whether a `done` task is delivered goes through this or the clause."""
    ids = list(task_ids)
    if not ids:
        return set()
    return set(session.scalars(select(Task.id).where(Task.id.in_(ids), awaiting_lane_merge_clause(Task.id))).all())


#: The `task_note` saying why the lane sent a runner back to its PR, above the reopen's `run` row (or alone, for a
#: live runner it only messaged), or that it resolved the PR's additive conflict itself and sent nobody back
#: (`REDISPATCH_RESOLVED`); `data.cause` is a `REDISPATCH_LABELS` key, `data.pr` the PR number.
REDISPATCH_NOTE = "lane_redispatch"
REDISPATCH_CONFLICT, REDISPATCH_FAILED, REDISPATCH_RESOLVED = "conflict", "failed", "resolved"
REDISPATCH_LABELS = {
    REDISPATCH_CONFLICT: "PR #{pr} conflicts — rebasing",
    REDISPATCH_FAILED: "PR #{pr} failed the lane gate — fixing",
    REDISPATCH_RESOLVED: "PR #{pr} conflicts — lane resolved",
}


def redispatch_label(data: Mapping[str, Any]) -> str:
    """The timeline label of a `REDISPATCH_NOTE` row. An unknown cause reads as the failure: never a raise on an
    append-only row."""
    label = REDISPATCH_LABELS.get(str(data.get("cause")), REDISPATCH_LABELS[REDISPATCH_FAILED])
    return label.format(pr=data.get("pr"))


# ── a lane run's wall time and gate count (`run_stats`) ──

#: `tasks.metrics`: the lane run's own `run_stats` (`merge_lane.apply_event`), read back as the wire's `lane_run`.
RUN_STATS_KEY = "merge_lane_run"
#: A run is slow past twice the project's last full gate, never below this.
LANE_SLOW_FLOOR_S = 600
#: The full gate's seconds assumed while no lane run of the project has timed one.
LANE_FULL_GATE_FALLBACK_S = 3600


def last_full_gate_seconds(session: Session, project_ids: Iterable[int]) -> dict[int, int]:
    """Each project's newest stored `last_full_gate_s` among its lane runs, one query; absent when none timed one."""
    ids = sorted({int(i) for i in project_ids if i is not None})
    if not ids:
        return {}
    seconds = Task.metrics[RUN_STATS_KEY]["last_full_gate_s"].astext
    rows = session.execute(
        select(Task.project_id, seconds)
        .where(Task.project_id.in_(ids), Task.persona == LANE_PERSONA, seconds.is_not(None))
        .distinct(Task.project_id).order_by(Task.project_id, Task.id.desc())
    ).all()
    return {int(pid): int(float(s)) for pid, s in rows}


def lane_run_view(task: Task, last_full_gate_s: Optional[int], now: datetime) -> Optional[dict[str, Any]]:
    """`lane_run` for one lane task: its gates, its wall time (an active run's elapsed so far, computed now, so it
    grows with no new event) and `slow` past max(2 × the project's last full gate, LANE_SLOW_FLOOR_S). None for
    another persona or a run that has reported nothing yet."""
    stats = (task.metrics or {}).get(RUN_STATS_KEY) if task.persona == LANE_PERSONA else None
    if not isinstance(stats, Mapping):
        return None
    wall = int(stats.get("wall_s") or 0)
    if task.state == ACTIVE and not stats.get("final"):
        try:
            wall = max(wall, int((now - datetime.fromisoformat(str(stats["started_at"]))).total_seconds()))
        except (KeyError, ValueError, TypeError):
            pass
    bound = max(2 * (last_full_gate_s or LANE_FULL_GATE_FALLBACK_S), LANE_SLOW_FLOOR_S)
    return {"gates": int(stats.get("gates") or 0), "wall_s": wall, "slow": wall > bound}


def lane_runs(session: Session, tasks: Iterable[Task]) -> dict[int, dict[str, Any]]:
    """`lane_run_view` for every lane task among `tasks` with stats: one query for their projects' full gates."""
    lanes = [t for t in tasks
             if t.persona == LANE_PERSONA and isinstance((t.metrics or {}).get(RUN_STATS_KEY), Mapping)]
    if not lanes:
        return {}
    full = last_full_gate_seconds(session, [t.project_id for t in lanes])
    now = utcnow()
    views = {t.id: lane_run_view(t, full.get(t.project_id), now) for t in lanes}
    return {i: v for i, v in views.items() if v is not None}


def lane_run(session: Session, task: Task) -> Optional[dict[str, Any]]:
    """`lane_runs` for one task."""
    return lane_runs(session, [task]).get(task.id)


# ── Certificates every edition reads (the release executor's pin, `releases/runs.py`) ─────────────────────────


def gate_cmd_hash(gate_full_cmd: Optional[str]) -> str:
    """`sha256(gate_full_cmd.strip())` — the gate a certificate was earned under."""
    return hashlib.sha256((gate_full_cmd or "").strip().encode()).hexdigest()


#: A base-check run's `Task.metrics` key naming the integration-branch commit it gates (`merge_lane.BASE_CHECK_KEY`).
BASE_SHA_KEY = "merge_lane_base"
#: In a certification run's `metrics`: the branch head its toolbox gated in place of its filed commit (`tip` on the
#: `certified`/`develop_red` event, `merge_lane._follow_tip`); the run's `merge_lane_base` then names that head too.
TIP_SHA_KEY = "merge_lane_tip"
#: How many of the newest certificates `certified_pin` ranks by place on the branch: an older one cannot be further
#: along than all of them.
_PIN_CANDIDATES = 50


@dataclass(frozen=True)
class CertifiedPin:
    """The integration-branch commit a promote pins (`latest_certified_sha`): `sha`, when its certificate was earned
    (`certified_at`), when that commit was the branch's head (`head_at`), and how many lane merges into the branch
    came after it (`behind`; a hand merge the CP never saw is not counted) and their merge shas (`newer_shas`): what the
    pinned commit does not contain."""

    sha: str
    certified_at: datetime
    head_at: datetime
    behind: int
    newer_shas: frozenset[str] = frozenset()


def certified_pin(session: Session, project: Project) -> Optional[CertifiedPin]:
    """The certificate FURTHEST ALONG the integration branch under the project's CURRENT gate, or None. The CP asks
    no git, so a commit's place is when it was the branch's head: its lane merge's `merged_at`, else, for a tip a
    certification followed (`merge_lane._certify_idle`), when that run started, so before it fetched the tip, and for any
    other base check when it was filed. Creation order is not place: a
    base check certifies an older commit after a batch certified a newer one."""
    certs = list(session.execute(
        select(MergeLaneCertificate.develop_sha, MergeLaneCertificate.created_at, MergeLaneCertificate.lane_task_id)
        .where(MergeLaneCertificate.project_id == project.id,
               MergeLaneCertificate.gate_cmd_hash == current_hash(session, project))
        .order_by(MergeLaneCertificate.created_at.desc(), MergeLaneCertificate.id.desc())
        .limit(_PIN_CANDIDATES)
    ).all())
    if not certs:
        return None
    merged = dict(session.execute(
        select(MergeLaneRow.merge_sha, func.max(MergeLaneRow.merged_at))
        .where(MergeLaneRow.project_id == project.id, MergeLaneRow.state == MERGED,
               MergeLaneRow.base_branch == project.integration_branch, MergeLaneRow.merged_at.is_not(None),
               MergeLaneRow.merge_sha.in_({c.develop_sha for c in certs}))
        .group_by(MergeLaneRow.merge_sha)
    ).all())
    runs = {task_id: (metrics or {}, created) for task_id, metrics, created in session.execute(
        select(Task.id, Task.metrics, Task.created_at)
        .where(Task.id.in_({c.lane_task_id for c in certs if c.lane_task_id is not None}))
    ).all()}

    def head_at(cert: Any) -> datetime:
        if cert.develop_sha in merged:
            return merged[cert.develop_sha]
        if cert.lane_task_id not in runs:
            return cert.created_at
        metrics, created = runs[cert.lane_task_id]
        # A run that followed the tip fetched it as it started; any other gated a commit that was the head when the
        # run was filed (a batch's base), never later.
        return run_started(metrics, created) if metrics.get(TIP_SHA_KEY) == cert.develop_sha else created

    # `max` keeps the first of equals: the newest certificate wins a tie.
    best = max(certs, key=head_at)
    at = head_at(best)
    newer = list(session.scalars(select(MergeLaneRow.merge_sha).where(
        MergeLaneRow.project_id == project.id, MergeLaneRow.state == MERGED,
        MergeLaneRow.base_branch == project.integration_branch, MergeLaneRow.merged_at > at,
        MergeLaneRow.merge_sha.is_distinct_from(best.develop_sha))))
    return CertifiedPin(sha=best.develop_sha, certified_at=best.created_at, head_at=at, behind=len(newer),
                        newer_shas=frozenset(s for s in newer if s))


#: Why a pin lags the branch (`pin_stall`): the newest full check found the branch red, a full check is on its way, or
#: none has been filed since the merges after the pin (lane batches gated with a quick tier never certify).
STALL_RED, STALL_CHECKING, STALL_WAITING = "red", "checking", "waiting"


def pin_stall(session: Session, project: Project, pin: Optional[CertifiedPin]) -> Optional[dict[str, Any]]:
    """Why the certified pin has not advanced past lane merges into the branch, or None when nothing is behind it:
    `reason` (`STALL_RED` while the project's red-develop record under the current gate names a commit other than the
    pin, `STALL_CHECKING` while a base-check run is open, else `STALL_WAITING`: none filed since), `since` (when the pinned commit was the
    branch's head), and for red the red commit and how many tests fail on it."""
    if pin is None or pin.behind == 0:
        return None
    out: dict[str, Any] = {"reason": STALL_WAITING, "since": pin.head_at.isoformat(), "red_sha": None,
                           "failing": None, "check_task_id": None}
    red = project.merge_lane_develop_red
    if red and red.get("gate_cmd_hash") == current_hash(session, project) and red.get("develop_sha") != pin.sha:
        return {**out, "reason": STALL_RED, "red_sha": red.get("develop_sha"), "failing": len(red.get("tests") or [])}
    check = session.scalar(select(Task.id).where(
        Task.project_id == project.id, Task.persona == LANE_PERSONA, Task.state.not_in((DONE, SYSTEM_FAILED)),
        Task.metrics.has_key(BASE_SHA_KEY)).order_by(Task.id.desc()).limit(1))
    if check is not None:
        return {**out, "reason": STALL_CHECKING, "check_task_id": check}
    return out


def run_started(metrics: Optional[Mapping[str, Any]], created: datetime) -> datetime:
    """When a lane run began (its toolbox's first stats post, `RUN_STATS_KEY`), else when it was filed."""
    stats = (metrics or {}).get(RUN_STATS_KEY)
    try:
        started = datetime.fromisoformat(str(stats["started_at"])) if isinstance(stats, Mapping) else created
    except (KeyError, ValueError, TypeError):
        return created
    return started if started.tzinfo is not None else started.replace(tzinfo=timezone.utc)


def latest_certified_sha(session: Session, project: Project) -> Optional[str]:
    """The integration-branch commit a promote pins: the certificate furthest along the branch (`certified_pin`)."""
    pin = certified_pin(session, project)
    return pin.sha if pin is not None else None


def certified_tree(session: Session, project: Project, develop_sha: str) -> Optional[str]:
    """The tree the lane certified at integration-branch commit `develop_sha` under the CURRENT gate, or None."""
    return session.scalar(
        select(MergeLaneCertificate.tree_hash)
        .where(MergeLaneCertificate.project_id == project.id, MergeLaneCertificate.develop_sha == develop_sha,
               MergeLaneCertificate.gate_cmd_hash == current_hash(session, project))
        .order_by(MergeLaneCertificate.created_at.desc(), MergeLaneCertificate.id.desc())
        .limit(1)
    )
