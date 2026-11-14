"""The Pipeline page's reads: `view` (the stage strip and the steps between stages) and `items` (one stage's list,
or every stage as sections), both over one `Board` — the project's items placed by `membership.locate` — so a stage's
count is exactly the rows its list pages through.

`view` (architecture §4): each stage with its `count` (always present, 0 included; a merging item counts on its
merge step, `membership.ON_STEP_ONLY`), `failed` (its failed tasks; a failed merge is a retry, never counted),
`stopped` (its stopped tasks), `asking` (its rows asking: the task itself, or an epic's child — the list's "?") and
`live` (the version on it and since when, for a stage a release reached); Production's live version (`prod_live`); each step's live state
(`in_flight`, `failed`, `waiting`, `approvals`, the open or failed release, what the caller `can` do, the freeze);
and the `not_started`, `done` and `archived` counts (a bucket the request does not list is counted in SQL, never
loaded: `Board.load`'s `defer`). Read under the items read's filters (`Filters`), every count is
the listed rows'; a step's live state stays the project's, since a step acts on the whole project (a deploy carries
every change waiting).

`items`: rows `{key, stage_id, group, task, change, failure, automatic}` (`automatic`: a hidden task listed for the
change it merged, which the page folds), keyset-paged in screen order (`Board.sort_key`), with `total` and
`folded` (per stage or bucket, how many of the pick the page folds: `folded`);
a release-target stage adds its `releases`, newest first, the newest open and grouped by feature; under a filter only
the releases that carried a listed item, among the newest `RELEASES_SCANNED`, each with those items alone. A change
row never
carries a pull request.

A release's `version` is its merge commit's short sha (the control plane holds no application version).
"""
from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from app.authz.principal import Principal
from app.authz.visibility import has_project_key, scoped_task_select
from app.merge_lane_state import FAILED as LANE_FAILED, OPEN_STATES as LANE_OPEN
from app.models.production_qa import Deployment
from app.models.projects import Feature, Project
from app.models.releases import PipelineApproval, Release, ReleaseApproval, ReleaseStage
from app.models.tasks import Task
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, DRAFT, _FOCUS_TOP_LEVEL_STATES
from app.task_run_metrics import id_any
from app.clock import utcnow
from app.entity_shape import iso_or_none
from app.pipeline import gate as gate_mod, membership, reasons as reasons_mod
from app.pipeline.membership import Item, StagePlace
from app.pipeline.rules import StepRules
from app.releases import access, judge, lifecycle, pipeline as setup, service, snapshot, stages as stages_mod
from app.releases import arrivals as arrivals_mod, health as health_mod, waiting as waiting_mod
from app.pipeline_gates import defaults as gate_defaults
from app.routes.tasks.task_wire import _asking_task_keys, _gated_child_ids

ITEMS_LIMIT_MAX = 200
ITEMS_LIMIT_DEFAULT = 100
#: The releases a release-target stage lists (the newest open, the rest folded).
RELEASES_SHOWN = 20
#: The newest releases a narrowed board searches for ones carrying its items.
RELEASES_SCANNED = 200
_CURSOR_DETAIL = "after is not a cursor this listing issued"
#: The buckets a board may count in SQL instead of loading their rows (`Board.load`'s `defer`).
DEFERRABLE = frozenset({membership.DONE_BUCKET, membership.ARCHIVED})


@dataclass
class Filters:
    """The board's row filters (`routes/tasks/listing.py::board_filtered_select`'s keywords) an items read narrows by.
    A task-only filter (creator, assignee, source, state, period, focus) leaves no taskless change in the list."""

    q: Optional[str] = None
    created_by_id: Optional[list[UUID]] = None
    assigned_to_id: Optional[list[UUID]] = None
    source: Optional[list[str]] = None
    state: Optional[str] = None
    updated_after: Optional[datetime] = None
    updated_before: Optional[datetime] = None
    focus: bool = False
    #: A feature's id: that feature's tasks alone (`listing.py::_feature_filter`).
    feature_id: Optional[str] = None
    persona: Optional[str] = None
    #: The user's hidden features above the scope (`listing.py::_exclusion_clauses`, sent
    #: by the web from `lib/taskMute.ts`): their tasks, and the changes those tasks merged, leave the board. Not a
    #: narrowing (`narrows`): what is left reads as the whole board.
    exclude_feature_id: Optional[list[UUID]] = None

    def task_only(self) -> bool:
        return bool(self.created_by_id or self.assigned_to_id or self.source or self.state or self.updated_after
                    or self.updated_before or self.focus or self.feature_id or self.persona)

    def narrows(self) -> bool:
        return self.task_only() or bool(self.q and self.q.strip())


def _board_select(principal: Optional[Principal], project_id: int, *, include_hidden: bool,
                  filters: Optional[Filters] = None, hidden_features: frozenset[UUID] = frozenset()) -> Any:
    from app.routes.tasks.listing import board_filtered_select  # noqa: PLC0415 — the board's own row set

    f = filters or Filters()
    stmt, _number = board_filtered_select(
        principal, state=f.state, project_id=[project_id], feature_id=f.feature_id, created_by_id=f.created_by_id,
        persona=f.persona, q=f.q, include_hidden=include_hidden, include_archived=True, exclude_done=False,
        focus=f.focus, updated_after=f.updated_after, updated_before=f.updated_before, source=f.source,
        assigned_to_id=f.assigned_to_id, exclude_feature_id=sorted(hidden_features) or None,
    )
    return stmt


def _hidden_features(session: Session, project_id: int, filters: Optional[Filters]) -> frozenset[UUID]:
    """This project's features the filters exclude: each named one."""
    if filters is None or not filters.exclude_feature_id:
        return frozenset()
    named = set(filters.exclude_feature_id)
    rows = session.scalars(select(Feature.id).where(Feature.project_id == project_id)).all()
    return frozenset(fid for fid in rows if fid in named)


def _deferred_clauses(facts: membership.Facts, defer: frozenset[str]) -> dict[str, Any]:
    """`{bucket: SQL clause}`: the rows of a board select `locate` puts in that bucket whatever else is read, so a
    board that does not list the bucket counts them instead of loading them. Archived: `archived_at` set. Done: not
    archived, `done`, no merge sha (`Facts.shas_by_root`) and no open or failed lane row (`Facts.lane`). A task with
    a step press or approval pending (`Facts.pressed`) is always loaded: `_still_before` and `_pressed_drafts` read
    it as an item."""
    out: dict[str, Any] = {}
    if membership.ARCHIVED in defer:
        out[membership.ARCHIVED] = and_(Task.archived_at.is_not(None), not_(id_any(Task.id, facts.pressed)))
    if membership.DONE_BUCKET in defer:
        out[membership.DONE_BUCKET] = and_(
            Task.archived_at.is_(None), Task.state == DONE,
            not_(id_any(Task.id, set(facts.shas_by_root) | set(facts.lane) | facts.pressed)))
    return out


def _task_rows(session: Session, stmt: Any, facts: membership.Facts,
               defer: frozenset[str]) -> tuple[list[Item], set[int], dict[str, int]]:
    """A board select's task items less the rows `defer` counts in SQL (`_deferred_clauses`): the items, the deferred
    archived tasks that own a change (their ids: a change stays its task's, never a bare change), and each deferred
    bucket's count."""
    clauses = _deferred_clauses(facts, defer)
    if not clauses:
        return membership.task_items(session, stmt), set(), {}
    items = membership.task_items(session, stmt.where(not_(or_(*clauses.values()))))
    counted = stmt.where(or_(*clauses.values())).with_only_columns(Task.archived_at).subquery()
    archived_n, done_n = session.execute(select(
        func.count().filter(counted.c.archived_at.is_not(None)),
        func.count().filter(counted.c.archived_at.is_(None)))).one()
    counts = {bucket: n for bucket, n in ((membership.ARCHIVED, archived_n), (membership.DONE_BUCKET, done_n))
              if bucket in clauses}
    owners: set[int] = set()
    if membership.ARCHIVED in clauses and facts.shas_by_root:
        owners = set(session.scalars(stmt.where(clauses[membership.ARCHIVED], id_any(Task.id, facts.shas_by_root))
                                     .with_only_columns(Task.id)))
    return items, owners, counts


def _pressed_drafts(session: Session, principal: Optional[Principal], project_id: int, ids: set[int]) -> list[Item]:
    """Another member's draft whose step was pressed and waits for approvals: the board hides other people's drafts
    (`authz/visibility.py::board_draft_clause`), but an approver must see what waits on them. Still only a task the
    caller may see (`scoped_task_select`)."""
    if not ids:
        return []
    return membership.task_items(session, scoped_task_select(principal).where(
        Task.project_id == project_id, Task.parent_task_id.is_(None), Task.id.in_(sorted(ids)),
        Task.unsaved.is_(False), Task.state == DRAFT, Task.hidden.is_(False)))


def _automatic_tasks(session: Session, principal: Optional[Principal], project_id: int, facts: membership.Facts,
                     owned: set[int]) -> list[Item]:
    """The top-level tasks off the board (hidden: automatic work such as memory notes or docs refreshes) that merged a
    change: listed as tasks, flagged `automatic` so a stage folds them, never as a bare change. Still only a task the
    caller may see (`scoped_task_select`); with Hidden shown they are on the board already (`owned`)."""
    roots = {facts.roots.get(c.task_id, c.task_id) for c in facts.changes if c.task_id is not None} - owned
    if not roots:
        return []
    return [replace(item, automatic=True) for item in membership.task_items(session, scoped_task_select(principal).where(
        Task.project_id == project_id, Task.parent_task_id.is_(None), Task.id.in_(sorted(roots)),
        Task.unsaved.is_(False)))]


def _change_matches(item: Item, q: Optional[str]) -> bool:
    words = (q or "").casefold().split()
    title = item.title.casefold()
    return all(w in title or (item.change is not None and item.change.merge_sha.startswith(w)) for w in words)


def _in_focus(item: Item, place: StagePlace, leaving: set[UUID]) -> bool:
    """Focus keeps what is still being worked (a task in `_FOCUS_TOP_LEVEL_STATES`, as the task list's Focus) and
    anything a release still has to ship: a row on a stage in `leaving` (the branch stages a release leaves from,
    today's Dev), finished or taskless, so Focus never empties that stage or its Release list. Never an archived row."""
    if item.archived:
        return False
    if item.task_id is not None and item.state in _FOCUS_TOP_LEVEL_STATES:
        return True
    return place.bucket is None and place.stage_id in leaving


@dataclass
class Board:
    """One project's items, placed. Built once per request (`load`). A deferred bucket's rows (`defer`) are not in
    `items`: `deferred` counts them, and `archived_owners` names the deferred archived ones that own a change."""

    session: Session
    project: Project
    principal: Optional[Principal]
    stages: list[ReleaseStage]
    facts: membership.Facts
    items: list[Item]
    places: dict[str, StagePlace]
    owned: set[int] = field(default_factory=set)
    #: Whether a filter or a hidden feature narrowed `items`: a release then lists only the items it carried that the
    #: filters keep.
    narrowed: bool = False
    #: `{bucket: rows}` counted in SQL, not loaded (`load`'s `defer`).
    deferred: dict[str, int] = field(default_factory=dict)
    #: The deferred archived tasks of the list that own a change: a release lists them (`release_items`).
    archived_owners: set[int] = field(default_factory=set)
    #: The task select `items` came from (`release_items` loads `archived_owners` through it).
    task_select: Any = None

    @classmethod
    def load(cls, session: Session, project: Project, principal: Optional[Principal], *,
             include_hidden: bool = False, filters: Optional[Filters] = None,
             defer: frozenset[str] = frozenset()) -> "Board":
        """`defer` (⊆ `DEFERRABLE`): the buckets the request does not list, counted in SQL instead of loaded and
        placed one by one (`_deferred_clauses`); a board that lists a bucket must not defer it."""
        stages = gate_mod.stages_of(session, project)
        facts = membership.Facts.load(session, project, stages)
        hidden = _hidden_features(session, project.id, filters)
        base_select = _board_select(principal, project.id, include_hidden=include_hidden, hidden_features=hidden)
        base, base_owners, deferred = _task_rows(session, base_select, facts, defer)
        pressed = [p for p in _pressed_drafts(session, principal, project.id, facts.pressed - {i.task_id for i in base})
                   if p.feature_id not in hidden]
        owned = {i.task_id for i in [*base, *pressed] if i.task_id is not None} | base_owners
        automatic = _automatic_tasks(session, principal, project.id, facts, owned)
        # A hidden feature's task that merged a change takes the change with it: never a bare change in its place.
        muted = {i.task_id for i in automatic if i.task_id is not None and i.feature_id in hidden}
        automatic = [a for a in automatic if a.task_id not in muted]
        owned |= {i.task_id for i in automatic if i.task_id is not None}
        changes = membership.change_items(facts, owned | muted)
        narrowed = filters is not None and filters.narrows()
        # Focus is applied after placing (`_in_focus`): what waits to release stays, whatever its state.
        rest = replace(filters, focus=False) if filters is not None else None
        tasks_select, owners = base_select, base_owners
        if rest is not None and rest.narrows():
            tasks_select = _board_select(principal, project.id, include_hidden=include_hidden, filters=rest,
                                         hidden_features=hidden)
            tasks, owners, deferred = _task_rows(session, tasks_select, facts, defer)
            # A filter can name rows the default board leaves out (a planner task by persona): theirs too.
            owned |= {i.task_id for i in tasks if i.task_id is not None} | owners
            pressed = [] if rest.task_only() else [p for p in pressed if _change_matches(p, rest.q)]
            automatic = [] if rest.task_only() else [a for a in automatic if _change_matches(a, rest.q)]
            changes = [] if rest.task_only() else [c for c in changes if _change_matches(c, rest.q)]
        else:
            tasks = base
        tasks = [*tasks, *pressed, *automatic]
        items = [*tasks, *changes]
        places = membership.locate(project, stages, items, facts)
        if filters is not None and filters.focus:
            leaving = {s.id for s in stages_mod.branch_stages(stages)[:-1]}
            items = [i for i in items if _in_focus(i, places[i.key], leaving)]
            places = {i.key: places[i.key] for i in items}
            # Focus keeps no archived row, and a bucket row only in a focus state, which `done` is not.
            deferred, owners = {bucket: 0 for bucket in deferred}, set()
        # A release then lists, counts and groups only what is left: never a hidden task's change.
        return cls(session, project, principal, stages, facts, items, places, owned, narrowed or bool(hidden),
                   deferred, owners, tasks_select)

    @cached_property
    def release_items(self) -> list[Item]:
        """`items` and the deferred archived tasks that own a change (`archived_owners`): every item a release can
        list. Those are loaded and placed on first use; `counts` never reads them."""
        if not self.archived_owners:
            return self.items
        more = membership.task_items(self.session, self.task_select.where(id_any(Task.id, self.archived_owners)))
        self.places.update(membership.locate(self.project, self.stages, more, self.facts))
        return [*self.items, *more]

    @cached_property
    def asking(self) -> set[str]:
        """The keys the list marks asking ("?"): `_asking_keys`, read once per request."""
        return _asking_keys(self)

    def sort_key(self, item: Item) -> tuple[Any, ...]:
        """Screen order (`StagePlace.sort_key`), every "?" row on top of its stage."""
        return self.places[item.key].sort_key(item, item.key in self.asking)

    # ── counts ───────────────────────────────────────────────────────────────────────────────────────

    def counts(self) -> dict[Any, int]:
        out: dict[Any, int] = {s.id: 0 for s in self.stages}
        out.update({b: self.deferred.get(b, 0) for b in membership.BUCKETS})
        for place in (self.places[i.key] for i in self.items):
            if place.bucket is None and place.group in membership.ON_STEP_ONLY:
                continue  # counted on its step (`_step_out`), listed under that count
            out[place.stage_id if place.bucket is None else place.bucket] += 1
        return out


# ── view ──────────────────────────────────────────────────────────────────────────────────────────────

def version_of(release: Release) -> str:
    """What people call a release (`runs.stamp_version`); "" until it is pinned. Never a commit hash."""
    return release.version or ""


def runs_of(session: Session, project_id: int, releases: Sequence[Release]) -> dict[int, dict[str, Any]]:
    """`{release id: run}` for the releases whose deploy run is linked, in one query: the run page's id
    (`/deployments/:id`), its status and where it failed. A run of another project is never shown."""
    ids = {r.deployment_id for r in releases if r.deployment_id is not None}
    if not ids:
        return {}
    runs = {d.id: {"id": d.id, "status": d.status, "failing_job": d.failing_job, "failing_step": d.failing_step}
            for d in session.scalars(select(Deployment).where(Deployment.id.in_(ids),
                                                              Deployment.project_id == project_id))}
    return {r.id: runs[r.deployment_id] for r in releases if r.deployment_id in runs}


def _release_brief(session: Session, release: Release) -> dict[str, Any]:
    return {
        "id": release.id, "state": release.state, "version": version_of(release),
        "scheduled_for": iso_or_none(release.scheduled_for), "failure": release.failure, "run_task_id": release.task_id,
        "run": runs_of(session, release.project_id, [release]).get(release.id),
    }


def _latest_by_target(session: Session, project_id: int, states: Optional[Sequence[str]] = None) -> dict[UUID, Release]:
    stmt = select(Release).where(Release.project_id == project_id, Release.to_stage_id.is_not(None))
    if states is not None:
        stmt = stmt.where(Release.state.in_(tuple(states)))
    stmt = (stmt.distinct(Release.to_stage_id).order_by(Release.to_stage_id, Release.id.desc()))
    return {r.to_stage_id: r for r in session.scalars(stmt)}


def _pending_by_stage(session: Session, project_id: int) -> dict[UUID, dict[tuple[Any, Any], int]]:
    """`{stage id: {(task id, sha): approvals}}`: the subjects with a press or approval pending on each step."""
    out: dict[UUID, dict[tuple[Any, Any], int]] = {}
    for stage_id, task_id, sha, kind in session.execute(
        select(PipelineApproval.stage_id, PipelineApproval.task_id, PipelineApproval.merge_sha, PipelineApproval.kind)
        .where(PipelineApproval.project_id == project_id)
    ):
        subjects = out.setdefault(stage_id, {})
        subjects[(task_id, sha)] = subjects.get((task_id, sha), 0) + (kind == gate_mod.APPROVE)
    return out


#: The states in which the list's row shows a question at all (`components/tasks/shared.tsx::GATE_LIVE_STATES`).
_GATE_LIVE_STATES = frozenset({ACTIVE, AWAITING_RESPONSE})


def _asking_keys(board: Board) -> set[str]:
    """The keys of the board's tasks the list marks asking ("?"): a live row with its own open question (a parked
    one whose answer is delivering reads "Delivering", not "?"), or with an asking child (`task_wire._asking_task_keys`
    / `_gated_child_ids`, the rule the list's row reads)."""
    rows = [(i.task_id, i.state, i.updated_at) for i in board.items
            if i.task_id is not None and i.state in _GATE_LIVE_STATES and i.updated_at is not None]
    if not rows:
        return set()
    ids = set(_asking_task_keys(board.session, rows))
    ids |= set(_gated_child_ids(board.session, [r[0] for r in rows], board.principal))
    return {membership.task_key(task_id) for task_id in ids}


def view(board: Board) -> dict[str, Any]:
    """`GET …/pipeline/view` (module docstring)."""
    session, project = board.session, board.project
    counts = board.counts()
    failed: dict[UUID, int] = {s.id: 0 for s in board.stages}
    stopped: dict[UUID, int] = {s.id: 0 for s in board.stages}
    asking: dict[UUID, int] = {s.id: 0 for s in board.stages}
    lane: dict[str, int] = {"in_flight": 0, "failed": 0, "held": 0}
    asking_keys = board.asking
    for key, place in board.places.items():
        # Failed tasks only: a failed merge is a retry, its task Working (`membership._group`).
        if place.bucket is None and place.group == membership.FAILED:
            failed[place.stage_id] += 1  # type: ignore[index]
        if place.bucket is None and place.group == membership.STOPPED:
            stopped[place.stage_id] += 1  # type: ignore[index]
        if place.bucket is None and key in asking_keys:
            asking[place.stage_id] += 1  # type: ignore[index]
        if place.bucket == membership.ARCHIVED:
            continue
        if place.lane_state in LANE_OPEN and place.held:
            lane["held"] += 1
        elif place.bucket is None and place.group in membership.ON_STEP_ONLY:
            # What its pick lists (`select_items`): an asking or failed task with an open merge stays on its stage.
            lane["in_flight"] += 1
        elif place.lane_state == LANE_FAILED:
            lane["failed"] += 1
    arrived = _latest_by_target(session, project.id, tuple(lifecycle.ARRIVED))
    latest = _latest_by_target(session, project.id)
    # Several may wait into one stage (R6): the step shows the newest (oldest first, so the last one wins).
    open_by_target = {r.to_stage_id: r for r in service.open_releases(session, project.id) if r.to_stage_id}
    live = {sid: {"version": version_of(r), "since": iso_or_none(r.updated_at)} for sid, r in arrived.items()}
    production = stages_mod.production_stage(board.stages)
    pending = _pending_by_stage(session, project.id)
    branch = stages_mod.branch_stages(board.stages)
    waiting_changes = waiting_mod.waiting(session, project, branch, board.facts.changes) if len(branch) > 1 else {}
    steps = []
    for step in gate_mod.steps_of(board.stages):
        closed = _step_closed(board, step, branch)
        steps.append({**_step_out(board, step, lane, pending, open_by_target, latest, waiting_changes, branch, closed),
                      "open": closed is None, "closed_reason": closed[1] if closed is not None else None})
    arrivals = arrivals_mod.board_out(session, project, branch, board.principal)
    health = health_mod.rows_of(session, [s.id for s in board.stages])
    return {
        "stages": [{"id": str(s.id), "name": s.name, "count": counts[s.id], "failed": failed[s.id],
                    "stopped": stopped[s.id], "asking": asking[s.id], "live": live.get(s.id),
                    "arrival": arrivals.get(s.id),
                    # The health dot (`app/releases/health.py`): null without an address; `target` names where the
                    # address comes from on a stage that deploys, null on one that never has an address.
                    "health": health_mod.out(s, health.get(s.id)),
                    "target": gate_defaults.target_kind(s) if s.level == stages_mod.BRANCH else None}
                   for s in board.stages],
        "prod_live": ({"stage_id": str(production.id), **live[production.id]}
                      if production is not None and production.id in live else None),
        "steps": steps,
        "not_started": counts[membership.NOT_STARTED],
        "done": counts[membership.DONE_BUCKET],
        "archived": counts[membership.ARCHIVED],
    }


def _step_closed(board: Board, step: gate_mod.Step, branch: list[ReleaseStage]) -> Optional[tuple[str, str]]:
    """Why a release step may not run now (verify after arrival, `app/releases/arrivals.py::step_closed`): its chip's
    words and the step's `closed_reason`; None while it may, and on every other step."""
    if step.action != setup.RELEASE:
        return None
    source = next((s for s in reversed(branch) if board.stages.index(s) < step.index), None)
    if source is None:
        return None
    return arrivals_mod.step_closed(board.session, board.project, source)


def _step_out(board: Board, step: gate_mod.Step, lane: dict[str, int],
              pending: dict[UUID, dict[tuple[Any, Any], int]], open_by_target: dict[UUID, Release],
              latest: dict[UUID, Release], waiting_changes: dict[UUID, list[Any]],
              branch: list[ReleaseStage], closed: Optional[tuple[str, str]] = None) -> dict[str, Any]:
    session, project = board.session, board.project
    # The `can` flags answer as the write will run (`access.as_writer`), never offering what `act` refuses.
    principal = access.as_writer(board.principal, project.org_id)
    target = step.target
    can_act = gate_mod.may_act(principal, project.id, step) if principal is not None else False
    out: dict[str, Any] = {
        "to_stage_id": str(target.id), "action": step.action, "in_flight": 0, "failed": 0, "waiting": 0,
        "approvals": {"have": 0, "need": int(target.approvals_required or 0)}, "release": None,
        "can": {"act": can_act, "approve": False, "reject": False, "cancel": False, "retry": False,
                "schedule": False, "hotfix": False,
                # Run a press through a freeze (R7.5): who may press AND edit the pipeline; Resume a paused step (R8.3).
                "override": bool(can_act and has_project_key(principal, access.PROJECTS_EDIT, project.id)),
                "resume": False},
        "freeze": None, "gate": None,
        # Why the step waits, in display order (`app/pipeline/reasons.py`; the wire union is `pipelineBoard.ts`'s
        # `WaitReason`). `approvals`, `freeze` and `gate` above stay for an older web build.
        "reasons": [],
    }
    if step.action == setup.RELEASE:
        source = next((s for s in reversed(branch) if board.stages.index(s) < step.index), None)
        open_ = open_by_target.get(target.id)
        last = latest.get(target.id)
        shown = open_ if open_ is not None else (last if last is not None and last.state == lifecycle.FAILED
                                                 else None)
        rule = snapshot.rule(shown, target) if shown is not None else target
        approvals: list[gate_mod.Approval] = []
        if open_ is not None:
            approvals = [gate_mod.Approval(a.user_id, a.comment, a.approved_at) for a in session.scalars(
                select(ReleaseApproval).where(ReleaseApproval.release_id == open_.id).order_by(ReleaseApproval.id))]
        approved_by = {str(a.user_id) for a in approvals}
        if open_ is not None:
            out["in_flight"] = len(waiting_mod.carried(session, project, [open_.id], board.facts.changes)[open_.id])
        out["failed"] = 1 if open_ is None and last is not None and last.state == lifecycle.FAILED else 0
        out["waiting"] = len(waiting_changes.get(source.id, [])) if source is not None else 0
        out["approvals"] = {"have": len(approved_by), "need": int((rule or target).approvals_required or 0)}
        out["release"] = _release_brief(session, shown) if shown is not None else None
        user = str(getattr(principal, "user_id", ""))
        awaiting = open_ is not None and rule is not None and open_.state == lifecycle.AWAITING_APPROVAL
        may_approve = bool(awaiting and rule is not None and open_ is not None
                           and access.may_approve(principal, project.id, rule, open_))
        out["can"].update({
            "act": bool(principal is not None and access.may_deploy(principal, project.id, target)),
            "approve": may_approve and user not in approved_by,
            "reject": may_approve,
            "cancel": bool(open_ is not None and rule is not None
                           and access.may_cancel(principal, project.id, rule, open_)),
            "retry": bool(out["failed"] and access.may_deploy(principal, project.id, target)),
            "schedule": bool(principal is not None and access.may_deploy(principal, project.id, target)),
            "override": bool(principal is not None and access.may_deploy(principal, project.id, target)
                             and has_project_key(principal, access.PROJECTS_EDIT, project.id)),
            "hotfix": bool(principal is not None and target.id == getattr(stages_mod.production_stage(board.stages), "id", None)
                           and len(branch) > 1 and access.may_deploy(principal, project.id, target)),
        })
        running = open_ is not None and open_.state in (lifecycle.MERGING, lifecycle.DEPLOYING)
        # A running release always finishes: no freeze holds it, so none is named.
        timed = [] if running else judge.reasons(
            session, project, target, utcnow(), open_, source=source,
            shas=[c.merge_sha for c in waiting_changes.get(source.id, [])] if source is not None else [])
        held = next((r for r in timed if r.kind == "freeze"), None)
        out["freeze"] = held.data["until"] if held is not None else None
        missing = gate_mod.signoff_missing(session, project, target.id) if step.source.level == stages_mod.NONE else 0
        if missing:
            out["gate"] = gate_mod.gate_out(gate_mod.WaitSignOff(missing))
        pause = judge.paused(session, project, board.stages, target) if open_ is None else None
        out["can"]["resume"] = bool(pause is not None and out["can"]["act"])
        out["reasons"] = [reasons_mod.out(r) for r in reasons_mod.ordered([
            *timed,
            reasons_mod.of("paused", **pause.reason_data(session)) if pause is not None else None,
            # Closed for verify (`_step_closed`): its chip, as the Release button's hover says it.
            reasons_mod.of("verify", chip=closed[0], text=closed[1]) if closed is not None and not running else None,
            gate_mod.press_reason(session, step) if open_ is None and out["waiting"] and not target.auto_deploy
            else None,
            *(_release_approval_reasons(board, step, rule, open_, approvals, principal)
              if awaiting and rule is not None and open_ is not None else ()),
            reasons_mod.of("sign_off", count=missing) if missing else None,
        ])]
        return out
    subjects = pending.get(target.id, {})
    if step.action in (setup.MERGE, setup.START_MERGE):
        out["in_flight"], out["failed"], out["waiting"] = lane["in_flight"], lane["failed"], lane["held"]
    else:
        out["waiting"] = sum(1 for (task_id, sha) in subjects
                             if _still_before(board, task_id, sha, step.index))
    out["approvals"]["have"] = max(subjects.values(), default=0)
    may_approve = bool(target.approvals_required) and gate_mod.may_approve(principal, project.id, step)
    waiting = [gate_mod.Subject(task_id=t, sha=s) for (t, s) in subjects if _still_before(board, t, s, step.index)]
    user_id = getattr(principal, "user_id", None)
    # R2: a requester sees no Approve on a step where every waiting item is one they asked for.
    own = may_approve and StepRules.of(target).requester_cant_approve and bool(waiting) and user_id is not None and all(
        user_id in asked for asked in gate_mod.askers_of(session, step, waiting).values())
    out["can"]["approve"] = may_approve and not own
    out["can"]["reject"] = may_approve and bool(waiting) and not own
    if out["waiting"]:
        # The step's approval reasons are those of its furthest-along item (the one `have` counts).
        focus = max(waiting, key=lambda w: subjects.get((w.task_id, w.sha), 0), default=None)
        now = utcnow()
        # A Merge waits for a freeze on the integration stage as a press faces it (R7.1).
        merging = step.action in (setup.MERGE, setup.START_MERGE)
        out["reasons"] = [reasons_mod.out(r) for r in reasons_mod.ordered([
            judge.freeze_reason(session, project, target, now, pressed=True) if merging else None,
            gate_mod.press_reason(session, step) if not target.auto_deploy else None,
            *(gate_mod.subject_reasons(session, project, step, focus, principal) if focus is not None else ()),
            _soonest_soak(board, step, subjects, now),
            gate_mod.hours_reason(step, now),
        ])]
    return out


def _release_approval_reasons(board: Board, step: gate_mod.Step, rule: ReleaseStage, release: Release,
                              approvals: list[gate_mod.Approval], principal: Optional[Principal]) -> list[Any]:
    """A release waiting for approvals: `approvals` (who may, who did and what they said, the viewer's standing),
    the step's `instructions` and its `timeout`, all on the rule it was requested under."""
    session, project = board.session, board.project
    user = str(getattr(principal, "user_id", ""))
    viewer = gate_mod.viewer_state(principal, project.id, rule, step.action,
                                   approved=any(str(a.user_id) == user for a in approvals),
                                   requester=release.requested_by_id is not None and str(release.requested_by_id) == user)
    return [gate_mod.approvals_reason(session, project, rule, step.action, len(approvals),
                                      int(rule.approvals_required or 0), approvals, viewer),
            gate_mod.instructions_reason(rule), gate_mod.timeout_reason(rule, release.requested_at)]


def _soonest_soak(board: Board, step: gate_mod.Step, subjects: dict[tuple[Any, Any], int],
                  now: datetime) -> Optional[reasons_mod.Reason]:
    """The step's soak reason (R4) for the item it lets through first, among those still before it."""
    if not StepRules.of(step.target).soak_minutes:
        return None
    found = [r for (task_id, sha) in subjects if _still_before(board, task_id, sha, step.index)
             for r in [gate_mod.soak_reason(board.session, step, gate_mod.Subject(task_id=task_id, sha=sha), now)]
             if r is not None]
    return min(found, key=lambda r: r.data["until"], default=None)


def _still_before(board: Board, task_id: Optional[int], sha: Optional[str], index: int) -> bool:
    key = membership.task_key(task_id) if task_id is not None else membership.change_key(sha or "")
    place = board.places.get(key)
    return place is not None and place.bucket != membership.ARCHIVED and place.index < index


# ── items ─────────────────────────────────────────────────────────────────────────────────────────────

def _encode(sort: tuple[Any, ...]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(sort)).encode()).decode().rstrip("=")


def _decode(raw: str) -> tuple[Any, ...]:
    try:
        value = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=422, detail=_CURSOR_DETAIL) from None
    if not (isinstance(value, list) and len(value) == 4 and isinstance(value[0], int) and isinstance(value[1], int)
            and isinstance(value[2], (int, float)) and isinstance(value[3], str)):
        raise HTTPException(status_code=422, detail=_CURSOR_DETAIL)
    return tuple(value)


def stage_param(board: Board, stage: Optional[str]) -> tuple[Optional[UUID], Optional[str]]:
    """`?stage=` as a stage id or a bucket; 422 for anything else."""
    if stage is None or stage == "":
        return None, None
    if stage in membership.BUCKETS:
        return None, stage
    found = next((s for s in board.stages if str(s.id) == stage), None)
    if found is None:
        raise HTTPException(status_code=422, detail="stage must be a stage id of this pipeline, not_started, done "
                                                    "or archived")
    return found.id, None


def folded(item: Item) -> bool:
    """What a stage's list folds under Automatic: a hidden task listed for its change, and a taskless change."""
    return item.automatic or item.task_id is None


def select_items(board: Board, stage_id: Optional[UUID], bucket: Optional[str],
                 group: Optional[str] = None, *, with_folded: bool = True) -> list[Item]:
    """The items `?stage=`/`?group=` pick, in screen order: one stage, one bucket, or every stage (no bucket). A group
    shown only on its step (`membership.ON_STEP_ONLY`: Merging) is left out of a stage's list, and its own pick spans
    every stage, as its step's count does.
    `with_folded=False` leaves out what the list folds (`folded`): a tick-all over a closed fold names what it shows."""
    if bucket in board.deferred:
        raise ValueError(f"a board that lists {bucket} must load it (Board.load's defer)")
    places = board.places
    if stage_id is not None and group not in membership.ON_STEP_ONLY:
        picked = [i for i in board.items if places[i.key].stage_id == stage_id]
    elif bucket is not None:
        picked = [i for i in board.items if places[i.key].bucket == bucket]
    else:
        # Every stage; also a step's pick (`?group=merging`), which counts the whole project (`_step_out`).
        picked = [i for i in board.items if places[i.key].bucket is None]
    if group:
        picked = [i for i in picked if places[i.key].group == group]
    elif bucket is None:
        picked = [i for i in picked if places[i.key].group not in membership.ON_STEP_ONLY]
    if not with_folded:
        picked = [i for i in picked if not folded(i)]
    return sorted(picked, key=board.sort_key)


def page(board: Board, picked: list[Item], after: Optional[str], limit: int) -> tuple[list[Item], Optional[str]]:
    if after:
        cursor = _decode(after)
        picked = [i for i in picked if board.sort_key(i) > cursor]
    rows = picked[:limit]
    more = len(picked) > limit
    return rows, (_encode(board.sort_key(rows[-1])) if more and rows else None)


def _features(session: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    return dict(session.execute(select(Feature.id, Feature.name).where(Feature.id.in_(sorted(ids)))).all())


def rows_out(board: Board, rows: list[Item]) -> list[dict[str, Any]]:
    """The wire rows: a task as the board lists it (`TaskListOut`), a change as `{sha, title, merged_at, feature}`."""
    from app.routes.tasks.listing import board_rows_out  # noqa: PLC0415 — the board's own row shape

    session = board.session
    task_ids = [i.task_id for i in rows if i.task_id is not None]
    tasks = {t.id: t for t in session.scalars(select(Task).where(Task.id.in_(task_ids)))} if task_ids else {}
    ordered = [tasks[i] for i in task_ids if i in tasks]
    outs = {t.id: o for t, o in zip(ordered, board_rows_out(session, board.principal, ordered), strict=True)}
    change_features = _change_features(board, [i for i in rows if i.change is not None])
    out = []
    for item in rows:
        place = board.places[item.key]
        task = outs.get(item.task_id) if item.task_id is not None else None
        change = None
        if item.change is not None:
            change = {"sha": item.change.merge_sha, "title": item.change.title,
                      "merged_at": iso_or_none(item.change.merged_at), "feature": change_features.get(item.key)}
        out.append({
            "key": item.key, "stage_id": str(place.stage_id) if place.stage_id else None, "group": place.group,
            "task": task.model_dump(mode="json") if task is not None else None, "change": change,
            "failure": place.failure, "automatic": item.automatic,
        })
    return out


def _change_features(board: Board, items: list[Item]) -> dict[str, Optional[str]]:
    """A taskless change's feature: its own task's (a hidden or child task can carry one)."""
    task_ids = {i.change.task_id for i in items if i.change is not None and i.change.task_id is not None}
    if not task_ids:
        return {}
    by_task: dict[int, UUID] = {
        task_id: feature_id for task_id, feature_id in board.session.execute(
            select(Task.id, Task.feature_id).where(Task.id.in_(sorted(task_ids)))).all() if feature_id is not None}
    names = _features(board.session, set(by_task.values()))
    out: dict[str, Optional[str]] = {}
    for item in items:
        if item.change is not None and item.change.task_id in by_task:
            out[item.key] = names.get(by_task[item.change.task_id])
    return out


def _item_key_of(board: Board, change: waiting_mod.Change) -> str:
    root = board.facts.roots.get(change.task_id, change.task_id) if change.task_id is not None else None
    return membership.task_key(root) if root is not None and root in board.owned else \
        membership.change_key(change.merge_sha)


def release_features(board: Board, release_id: int, keys: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """The items one release carried (`keys`, when already read), grouped by feature: named features A→Z (deleted
    ones still named), then the items filed in the project itself, as a group named null."""
    if keys is None:
        keys = _carried_keys(board, [release_id])[release_id]
    by_key = {i.key: i for i in board.release_items}
    feature_ids = {f for k in keys if k in by_key and (f := by_key[k].feature_id) is not None}
    names = _features(board.session, feature_ids)
    change_features = _change_features(board, [by_key[k] for k in keys if k in by_key and by_key[k].change])
    groups: dict[Optional[str], list[str]] = {}
    for key in keys:
        item = by_key.get(key)
        name = (names.get(item.feature_id) if item is not None and item.feature_id is not None
                else change_features.get(key))
        groups.setdefault(name, []).append(key)
    named = sorted((n for n in groups if n is not None), key=str.casefold)
    order: list[Optional[str]] = [*named]
    if None in groups:
        order.append(None)
    return [{"name": n, "items": groups[n]} for n in order]


def release_parts(board: Board, release_id: int,
                  carried: Optional[list[waiting_mod.Change]] = None) -> dict[str, list[int]]:
    """`{epic id: its child tasks this release carries}` (`carried`: the release's changes, when already read): an epic
    listed for its children's merges ships only those parts, which the release's list shows under it (keys are
    strings: the wire's JSON object)."""
    if carried is None:
        carried = waiting_mod.carried(board.session, board.project, [release_id], board.facts.changes)[release_id]
    parts: dict[str, list[int]] = {}
    for change in carried:
        if change.task_id is None:
            continue
        root = board.facts.roots.get(change.task_id, change.task_id)
        if root != change.task_id:
            ids = parts.setdefault(str(root), [])
            if change.task_id not in ids:
                ids.append(change.task_id)
    return parts


def _carried_keys(board: Board, release_ids: list[int]) -> dict[int, list[str]]:
    """`{release id: the item keys it carried}`, in carried order; on a narrowed board only the keys it lists."""
    carried = waiting_mod.carried(board.session, board.project, release_ids, board.facts.changes)
    listed = {i.key for i in board.release_items} if board.narrowed else None
    return {rid: [k for k in dict.fromkeys(_item_key_of(board, c) for c in changes) if listed is None or k in listed]
            for rid, changes in carried.items()}


def releases_of(board: Board, stage: ReleaseStage) -> Optional[list[dict[str, Any]]]:
    """A release-target stage's releases that arrived, newest first: the newest `open` with its features, the rest
    folded (features loaded by `GET …/pipeline/releases/{id}/items`). None for any other stage. On a narrowed board
    only the releases that carried an item it lists, each counting those items alone."""
    if stage not in stages_mod.release_targets(board.stages):
        return None
    releases = list(board.session.scalars(
        select(Release).where(Release.project_id == board.project.id, Release.to_stage_id == stage.id,
                              Release.state.in_(tuple(lifecycle.ARRIVED)))
        .order_by(Release.id.desc()).limit(RELEASES_SCANNED if board.narrowed else RELEASES_SHOWN)))
    keys: dict[int, list[str]] = {}
    carried: dict[int, list[waiting_mod.Change]] = {}
    if board.narrowed:
        keys = _carried_keys(board, [r.id for r in releases])
        releases = [r for r in releases if keys[r.id]][:RELEASES_SHOWN]
        counts = {r.id: len(keys[r.id]) for r in releases}
    else:
        carried = waiting_mod.carried(board.session, board.project, [r.id for r in releases], board.facts.changes)
        counts = {r.id: len(carried[r.id]) for r in releases}
    runs = runs_of(board.session, board.project.id, releases)
    out = []
    for index, release in enumerate(releases):
        out.append({
            "id": release.id, "version": version_of(release), "at": iso_or_none(release.updated_at),
            "count": counts[release.id], "open": index == 0, "run": runs.get(release.id),
            "features": release_features(board, release.id, keys.get(release.id)) if index == 0 else [],
            "parts": release_parts(board, release.id, carried.get(release.id)) if index == 0 else {},
        })
    return out

