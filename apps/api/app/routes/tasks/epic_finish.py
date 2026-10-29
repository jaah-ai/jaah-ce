"""Epic finish: the finish phase, its follow-ups, the final report and acceptance checks."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from app import actors, merge_lane_state
from app.entity_shape import iso_or_none
from app.events import emit_console_event
from app.models.audit import ActivityEvent
from app.models.task_extras import ATTACHMENT_KIND_REPORT, TaskAttachment
from app.models.tasks import Task, TaskDependency
from app.short_title import short_title
from app.task_ancestry import executor_user_id
from app.task_artifacts import activity_row_is_visible, uploaded_clause, visible_artifact_clause
from app import task_prs
from app.task_lifecycle import (
    DONE,
    DRAFT,
    READY,
    _APPEND_FINISH_FOLLOW_UP_STATES,
    _EPIC_SETTLED_STATES,
    _FINISH_FOLLOW_UP_DEDUP_EXCLUDED_STATES,
)
from app import task_source
from app.task_settle import NO_BUILD_OUTCOME_REASON
from app.user_activity import record_task_note
from app.routes.tasks import dispatch, epics
from app.routes.tasks.policy import (
    EPIC_BUILD_PERSONAS,
    PERSONA_CODER,
    PERSONA_EPIC,
    PERSONA_FINISH,
    PERSONA_PLAN,
    _ANALYSIS_PERSONAS,
    _PLACEHOLDER_BODY_PERSONAS,
    epic_fanout_cap,
)
from app.routes.tasks.task_wire import _SETTLED_OUTPUT_NOTE_KINDS, _has_settled_output_body, _prs_out
from app.routes.tasks.dependencies import _would_create_cycle

log = logging.getLogger(__name__)


# ── Finish phase execution engine ───────────────────────────────────────────────────────────────────
# Division of labor: the finish RUNNER drives the server-computed settle with an
# EMPTY-body POST (it attests nothing), and jaahd's `sweep_finish_phases` is only a lease-gated crash
# backstop. Every fact in the report is computed HERE from the DB and persisted on the EPIC's `metrics`.

def list_finish_pending(session: Session) -> list[Task]:
    """Finish children (`persona=finish`, not yet `done`) whose non-finish siblings are ALL rollup-ready.
    The PRIMARY driver is `dispatchable_tasks` — finish is a dispatched runner role — so this worklist
    backs jaahd's CRASH BACKSTOP only: a finish child stuck `active` with an unheld lease keeps
    reappearing until the backstop settles it, so a crashed run cannot wedge the epic `active` forever."""
    # `archived_at IS NULL` on both queries: an archived finish child must never surface as a crash-backstop
    # candidate, and an archived sibling must not gate — or wrongly clear — another's rollup-readiness.
    candidates = session.scalars(
        select(Task).where(
            Task.persona == PERSONA_FINISH, Task.state != DONE, Task.archived_at.is_(None)
        )
    ).all()
    ready = []
    for finish in candidates:
        sibling_rows = session.execute(
            select(Task.state, Task.persona, Task.outcome_pr_url, Task.outcome_no_pr_reason, Task.id)
            .where(
                Task.parent_task_id == finish.parent_task_id, Task.id != finish.id,
                Task.archived_at.is_(None),
                # Excluded like an archived sibling: a follow-up is sequenced after this very finish child, so gating on
                # it is circular and a crashed finisher would never reappear. Matches `_is_undispatched_finish_follow_up`
                # in the report.
                Task.finish_follow_up_key.is_(None),
            )
        ).all()
        if sibling_rows and all(
            epics._child_rollup_ready(s, k, pr, no_pr) for s, k, pr, no_pr, _ in sibling_rows
        ) and not merge_lane_state.awaiting_lane_merge(session, [cid for *_, cid in sibling_rows]):
            ready.append(finish)
    return ready


def _finish_epic_children(epic_id: int, finish_id: int, session: Session) -> list[Task]:
    """This epic's other LIVE children — the single query feeding the finish stage's verify-resolution
    report, acceptance check, and time/token/efficiency metrics (all three read THIS list,
    never their own query), so an archived child can never resurface in a finish report."""
    return list(
        session.scalars(
            select(Task).where(
                Task.parent_task_id == epic_id, Task.id != finish_id, Task.archived_at.is_(None)
            ).order_by(Task.id)
        ).all()
    )


def _is_undispatched_finish_follow_up(child: Task) -> bool:
    """Is this child a Finish-filed follow-up that has NEVER been dispatched? One predicate shared by
    `_verify_resolution_report`, `_acceptance_check` and `_epic_token_metrics`.

    Sequenced after its finisher, such a row cannot have run when the report is computed, so scoring it
    as a failure reports a defect that does not exist. Keyed on the attempt count: once dispatched it is
    an ordinary `coder` child again."""
    return child.finish_follow_up_key is not None and not child.dispatch_attempts


# ── The epic's own final report ────────────────────────────────────────────────────────────────────
# An epic has an Artifacts tab but is never dispatched, so the CP copies one server-side on settle,
# with no agent tool: `artifact.upload` is `Scope.TASK`, so a runner still only uploads to itself.


def _pending_finish_follow_ups(
    epic_id: int, session: Session, exclude_id: Optional[int] = None
) -> list[int]:
    """The epic's Finish-filed follow-ups that have NOT settled yet. An empty list makes a child the epic's
    last word on the per-child edges; terminal backstops skip this (`ignore_pending`).

    Pending means not `_EPIC_SETTLED_STATES`, so a `system_failed` follow-up is finished (it will never
    run). Archived rows are excluded."""
    stmt = select(Task.id).where(
        Task.parent_task_id == epic_id,
        Task.finish_follow_up_key.is_not(None),
        Task.archived_at.is_(None),
        Task.state.not_in(_EPIC_SETTLED_STATES),
    )
    if exclude_id is not None:
        stmt = stmt.where(Task.id != exclude_id)
    return list(session.scalars(stmt))


def _publish_epic_final_report(
    child: Task, epic: Task, session: Session, *, ignore_pending: bool = False,
) -> Optional[int]:
    """Copy a settled child's newest report onto its EPIC as the epic's final report. Returns the new
    attachment id, or None when this child is not the one to publish. DOES NOT COMMIT — callers are the
    two per-child edges (`execute_finish_phase`, `_maybe_roll_up_parent`) and, via
    `_publish_epic_final_report_at_completion`, the two terminal backstops.

    Only the finish child (with no pending follow-up) or the last pending follow-up publishes, decided
    server-side — or, on an epic with nothing to build, the plan child that settled so (`NO_BUILD_OUTCOME_REASON`):
    no finish child ever exists there, and the plan document is the epic's deliverable. The child's
    most recent report wins (`app/dispatch_compose/` orders its uploads), and the `visible_artifact_clause`
    watermark is honoured.

    At most one copy per epic: the standing copy is replaced when the publisher is the same or a later
    child by `Task.id`. A heuristic with two accepted inversions (concurrent follow-ups; a finisher
    retried after its follow-up published). A NULL `source_task_id` is an orphan and always replaceable."""
    if child.state != DONE or epic.persona != PERSONA_EPIC or child.parent_task_id != epic.id:
        return None
    if epic.archived_at is not None:
        return None
    # No guard on the epic's state: a late re-drive onto a settled epic beats a `done` epic with an empty tab.

    # Archived or hidden publishes nothing: the epic's Artifacts tab is readable by project grantees
    # (`app/authz/visibility.py::visible_task_clause`), so a hidden child's artifact would cross that boundary.
    if child.archived_at is not None or child.hidden:
        return None
    if child.persona != PERSONA_FINISH and child.finish_follow_up_key is None and not _is_no_build_plan(child):
        return None

    # Every cheap predicate is above this line, so ordinary coder settles do not serialize on the epic row.

    # Row-lock before the pending read: otherwise two follow-ups settling at once could both see the other
    # pending (nobody publishes) or both see it done (both publish). Lock order is epic-then-child
    # everywhere (the INSERT takes `FOR KEY SHARE` on the child); never lock a child then its epic.
    session.get(Task, epic.id, with_for_update=True)
    # `ignore_pending` is the terminal callers' flag; a no-op at `_try_complete_epic` (nothing pending by
    # then) and load-bearing at the hand Complete, whose premise is a follow-up that will never run. Safe:
    # a later child still replaces.
    if not ignore_pending and _pending_finish_follow_ups(epic.id, session, exclude_id=child.id):
        return None
    # The standing copy, whole row: read directly, since a `SET NULL` source would blank a flag column.
    standing = session.scalars(
        select(TaskAttachment)
        .where(
            TaskAttachment.task_id == epic.id,
            TaskAttachment.kind == ATTACHMENT_KIND_REPORT,
        )
        .order_by(TaskAttachment.id.desc())
    ).first()
    if standing is not None:
        # An earlier child never overwrites a later one's word (see the docstring).
        if standing.source_task_id is not None and child.id < standing.source_task_id:
            return None
        session.delete(standing)
        session.flush()
    # Only metadata and a key to copy: the bytes live in S3, so publishing is one INSERT in the settle's
    # transaction.
    source = session.scalars(
        select(TaskAttachment)
        .where(
            TaskAttachment.task_id == child.id,
            TaskAttachment.kind == ATTACHMENT_KIND_REPORT,
            # A pending row is not publishable (`app/task_artifacts.py::uploaded_clause`): readers would filter it out.
            uploaded_clause(),
            visible_artifact_clause(TaskAttachment.created_at, child.artifacts_hidden_before),
        )
        # No loader options: an attachment row is only metadata and an S3 key.
        .order_by(TaskAttachment.created_at.desc(), TaskAttachment.id.desc())
    ).first()
    if source is None:
        return None
    # A copy of the row, so the report survives the child's deletion (`source_task_id` is `SET NULL`).
    # `uploaded_by_id` is the source's: the finish runner's executor. The object is shared, only the key copied (S3 keys
    # are never rewritten; `s3_key` is deliberately non-unique). Objects may be deleted only behind
    # `app/attachment_storage.py::reap_unreferenced_object`'s no-other-reference guard, never from here or
    # `_drop_epic_report_published_by`. `uploaded_at` is copied: it confirms bytes confirmed once, on the source.
    published = TaskAttachment(
        task_id=epic.id,
        kind=ATTACHMENT_KIND_REPORT,
        filename=source.filename,
        content_type=source.content_type,
        # The source row's own `size_bytes`, stamped by `complete_attachment` from what S3 actually received.
        size_bytes=source.size_bytes,
        s3_key=source.s3_key,
        s3_bucket=source.s3_bucket,
        uploaded_at=source.uploaded_at,
        etag=source.etag,
        uploaded_by_id=source.uploaded_by_id,
        source_task_id=child.id,
    )
    session.add(published)
    session.flush()
    log.info(
        "epic %s final report published from child task %s (attachment %s <- %s)",
        epic.id, child.id, published.id, source.id,
    )
    return published.id


def _drop_epic_report_published_by(task_id: int, session: Session) -> None:
    """PURGE the epic report this task published, if it published one. Does not commit.

    Called only where an attempt is destroyed: Reset and task delete. Retry does not purge: a failing
    re-run would empty a `done` epic's tab, and a stale report beats none. No child's watermark reaches this
    copy (it lives on the epic, whose own watermark only an epic Reset sets).

    Deletes the row, never the object: the key is shared with the child's report, and only
    `app/attachment_storage.py::reap_unreferenced_object` may remove objects. A hard delete (the copy is
    derived), scoped by `source_task_id`, taken before a task delete's `SET NULL` blanks the key.

    Takes no epic lock: a concurrent publish replaces rather than refuses, and `reset_task` already holds
    the child lock, so locking the epic here would invert the lock order and deadlock with `append_tasks`."""
    session.execute(
        delete(TaskAttachment).where(
            TaskAttachment.source_task_id == task_id,
            TaskAttachment.kind == ATTACHMENT_KIND_REPORT,
        )
    )


def _is_no_build_plan(child: Task) -> bool:
    """A plan child that settled `analysis` — nothing to build — which the settle stamped
    `NO_BUILD_OUTCOME_REASON`. Its report stands in for the finish child's on its epic."""
    return child.persona == PERSONA_PLAN and child.outcome_no_pr_reason == NO_BUILD_OUTCOME_REASON


def _publish_epic_final_report_at_completion(epic: Task, session: Session) -> Optional[int]:
    """The backstop, driven from `_try_complete_epic`: publish from the epic's last-settled qualifying child
    as the epic is about to complete. Does not commit.

    Covers a missed per-child edge, and a finisher whose pending follow-up was later archived. Ordered by
    `github_synced_at` then id — a heuristic, since that is a claim stamp on a finish child. Every
    candidate is tried, since one can qualify yet publish nothing; `_publish_epic_final_report` applies
    every predicate."""
    candidates = session.scalars(
        select(Task)
        .where(
            Task.parent_task_id == epic.id,
            Task.archived_at.is_(None),
            Task.hidden.is_(False),
            Task.state == DONE,
            or_(
                Task.persona == PERSONA_FINISH, Task.finish_follow_up_key.is_not(None),
                and_(Task.persona == PERSONA_PLAN, Task.outcome_no_pr_reason == NO_BUILD_OUTCOME_REASON),
            ),
        )
        .order_by(Task.github_synced_at.desc().nullslast(), Task.id.desc())
    ).all()
    for child in candidates:
        published = _publish_epic_final_report(child, epic, session, ignore_pending=True)
        if published is not None:
            return published
    return None


def _verify_resolution_report(
    epic_id: int,
    children: list[Task],
    session: Session,
    *,
    org: UUID,
) -> dict[str, Any]:
    """Itemized, REPORT-ONLY verify-resolution check — no remediation; an unresolved item is surfaced
    explicitly. (a) an analysis-artifact note per analysis child; (b) per-impl merged-or-no-pr, via
    `_child_rollup_ready`; (c) an orphan dispatch/fanout scan — a child stuck mid-fanout, or an epic
    whose plan→impl append never landed."""
    analysis_children = [c for c in children if c.persona in _ANALYSIS_PERSONAS]
    coders = [c for c in children if c.persona in EPIC_BUILD_PERSONAS]

    artifact_ids = {c.id for c in analysis_children}
    have_artifacts: set[int] = set()
    if artifact_ids:
        # Watermark applied in Python: a SQL join on `(data->>'id')::int` risks casting untyped JSONB text
        # before the filters narrow. A reset child's abandoned artifact counts as missing — this report is
        # persisted at the finisher's settle and never recomputed (`app/task_artifacts.py`).
        hidden_before = {c.id: c.artifacts_hidden_before for c in analysis_children}
        rows = session.execute(
            select(ActivityEvent.data["id"].astext, ActivityEvent.created_at)
            .where(
                # Either settled-output kind counts (`_SETTLED_OUTPUT_NOTE_KINDS`); the report is never recomputed.
                ActivityEvent.kind.in_(_SETTLED_OUTPUT_NOTE_KINDS),
                # A row is not a delivery: require the body (`_has_settled_output_body`), or this could call resolved
                # what `_latest_settled_output_note` renders as nothing.
                _has_settled_output_body(),
                ActivityEvent.data["id"].astext.in_([str(i) for i in artifact_ids]),
            )
        ).all()
        have_artifacts = {
            int(child_id) for child_id, created_at in rows
            if activity_row_is_visible(created_at, hidden_before.get(int(child_id)))
        }
    missing_artifacts = [
        {"child_id": c.id, "persona": c.persona} for c in analysis_children if c.id not in have_artifacts
    ]

    # A never-dispatched Finish-filed follow-up is not unresolved (`_is_undispatched_finish_follow_up`).
    # The report is persisted and never recomputed, so a wrong entry would show a red
    # `Verify: N unresolved` in FinishSection.tsx for ever.
    unresolved_impl = [
        {"child_id": c.id, "state": c.state}
        for c in coders
        if not _is_undispatched_finish_follow_up(c)
        and not epics._child_rollup_ready(c.state, c.persona, c.outcome_pr_url, c.outcome_no_pr_reason)
    ]

    # Not scored but not erased: every follow-up the finisher filed is listed as found and owned, whatever
    # its dispatch state. Informational only; absent from `all_resolved`.
    filed_follow_ups = [
        {"child_id": c.id, "state": c.state}
        for c in coders
        if c.finish_follow_up_key is not None
    ]

    # One read for the whole sweep: the setting's 3s cache could expire mid-loop and judge siblings against
    # two ceilings.
    fanout_ceiling = epic_fanout_cap(org=org)
    # Still `draft` and at the ceiling; a child readied by hand afterwards is not stuck.
    orphan_fanout = [
        {"child_id": c.id, "fanout_attempts": c.fanout_attempts}
        for c in children
        if c.state == DRAFT and c.fanout_attempts >= fanout_ceiling
    ]
    append_pending = epics._epic_append_pending(epic_id, session)

    return {
        "missing_analysis_artifacts": missing_artifacts,
        "unresolved_coders": unresolved_impl,
        "filed_follow_ups": filed_follow_ups,
        "orphan_fanout_children": orphan_fanout,
        "append_tasks_pending": append_pending,
        "all_resolved": not (missing_artifacts or unresolved_impl or orphan_fanout or append_pending),
    }


def _epic_time_metrics(epic_id: int, children: list[Task], session: Session) -> dict[str, Any]:
    """Total + per-phase time via an `ActivityEvent` interval query over the same `task:active`/`task:done`
    trail the activity log reads. Per child: first `task:active` → last `task:done`. Epic total: the epic's
    own first `task:active` (its "go" signal) → the LATEST `task:done` across every child."""
    ids = [epic_id] + [c.id for c in children]
    str_ids = [str(i) for i in ids]
    rows = session.execute(
        select(ActivityEvent.kind, ActivityEvent.data["id"].astext, ActivityEvent.created_at)
        .where(
            ActivityEvent.kind.in_(("task:active", "task:done")),
            ActivityEvent.data["id"].astext.in_(str_ids),
        )
        .order_by(ActivityEvent.created_at)
    ).all()
    first_active: dict[int, datetime] = {}
    last_done: dict[int, datetime] = {}
    for kind, id_str, created_at in rows:
        tid = int(id_str)
        if kind == "task:active" and tid not in first_active:
            first_active[tid] = created_at
        elif kind == "task:done":
            last_done[tid] = created_at  # ordered ascending — last write wins, keeping the LATEST

    per_child: dict[str, Any] = {}
    for c in children:
        start, end = first_active.get(c.id), last_done.get(c.id)
        per_child[str(c.id)] = {
            "persona": c.persona,
            "active_at": iso_or_none(start),
            "done_at": iso_or_none(end),
            "seconds": (end - start).total_seconds() if start and end and end >= start else None,
        }
    epic_start = first_active.get(epic_id)
    epic_end = max((v for v in last_done.values()), default=None)
    return {
        "epic_start_at": iso_or_none(epic_start),
        "epic_end_at": iso_or_none(epic_end),
        "total_seconds": (
            (epic_end - epic_start).total_seconds()
            if epic_start and epic_end and epic_end >= epic_start
            else None
        ),
        "per_child": per_child,
    }


def _epic_token_metrics(children: list[Task]) -> dict[str, Any]:
    """Captured `tokens_in`/`tokens_out` rolled up to an epic total AND a per-PHASE (`persona`) breakdown.
    Each phase carries `capture_status`: "missing" if any child lacks a captured row, else "captured",
    so a dropped capture is distinguishable from zero spend."""
    per_phase: dict[str, dict[str, Any]] = {}
    tokens_in = 0
    tokens_out = 0
    for c in children:
        # A never-dispatched Finish-filed follow-up contributes nothing, and no `capture_status: "missing"`
        # (see `_is_undispatched_finish_follow_up`).
        if _is_undispatched_finish_follow_up(c):
            continue
        phase = per_phase.setdefault(
            c.persona, {"tokens_in": 0, "tokens_out": 0, "capture_status": "captured"}
        )
        if c.metrics is None:
            phase["capture_status"] = "missing"
            continue
        c_in = int(c.metrics.get("tokens_in") or 0)
        c_out = int(c.metrics.get("tokens_out") or 0)
        phase["tokens_in"] += c_in
        phase["tokens_out"] += c_out
        tokens_in += c_in
        tokens_out += c_out
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "per_phase": per_phase}


def _epic_efficiency_benchmarks(children: list[Task], session: Session) -> dict[str, Any]:
    """Re-exploration and data-loss-between-phases benchmarks.
    Re-exploration: how many times the same file/seam
    target was independently re-mapped by more than one analysis stage. Data-loss: does each later stage's
    `task_dependencies` edge set reach EVERY earlier stage, or only its immediate predecessor?"""
    analysis_children = sorted((c for c in children if c.persona in _ANALYSIS_PERSONAS), key=lambda c: c.id)

    target_counts: dict[str, int] = {}
    for c in analysis_children:
        for target in set((c.metrics or {}).get("explored_targets") or []):
            target_counts[target] = target_counts.get(target, 0) + 1
    duplicated = {t: n for t, n in target_counts.items() if n > 1}
    re_exploration = {
        "duplicate_target_count": sum(n - 1 for n in duplicated.values()),
        "duplicated_targets": sorted(duplicated),
    }

    data_loss: list[dict[str, Any]] = []
    for idx, child in enumerate(analysis_children[1:], start=1):
        dep_ids = set(
            session.scalars(
                select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == child.id)
            ).all()
        )
        ancestor_ids = {a.id for a in analysis_children[:idx]}
        missing = ancestor_ids - dep_ids
        if missing:
            data_loss.append(
                {"child_id": child.id, "persona": child.persona, "missing_ancestor_ids": sorted(missing)}
            )

    return {"re_exploration": re_exploration, "data_loss_between_phases": data_loss}


# ── Acceptance-verification ────────────────────────────────────────────────────────────────────────
# The Finish phase compares the result against the epic's original requirement. `_acceptance_check` is
# a 3-tier oracle, strongest first: `ui_screenshot` (no producer exists, so honestly unsatisfied),
# `ac_named_check_green`, then `merge_completeness`. Report-only; it never blocks the phase.

# THE PARSER FOR AN AC-NAMED REQUIRED CHECK — the single home.
_REQUIRED_CHECKS_LINE_RE = re.compile(r"^[ \t]*Required checks:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _parse_required_checks(text: Optional[str]) -> list[str]:
    """Pure parser: the check names from a coder child body's explicit `Required checks: <name>[, ...]`
    line(s) — comma-split, each name trimmed, empties dropped, duplicates collapsed (first wins), original
    order preserved. `[]` when there is none."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _REQUIRED_CHECKS_LINE_RE.finditer(text):
        for raw in match.group(1).split(","):
            name = raw.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


# This repo's one UI deployable is `apps/web`, and most epics here are backend/infra-only, so a
# mention of it (or an explicit frontend/screenshot/UI callout) is a reliable, cheap signal. Heuristic-only
# by design — the question is only "does this epic deliver a visual surface", never which component.
_UI_SURFACE_RE = re.compile(r"apps/web|frontend|\bscreenshot\b|\bUI\b", re.IGNORECASE)


def _epic_has_ui_surface(epic: Task, children: list[Task]) -> bool:
    """Does this epic deliver a visual/UI surface? Checked against the epic's own body plus
    every child body carrying REAL content; any match is enough. The placeholder-body children are SKIPPED
    because their stored body is a dispatch-time placeholder, and the epic's own body is the true source."""
    child_texts = [c.body for c in children if c.persona not in _PLACEHOLDER_BODY_PERSONAS]
    texts = [epic.body] + child_texts
    return any(text and _UI_SURFACE_RE.search(text) for text in texts)


def _acceptance_check(epic: Task, children: list[Task]) -> dict[str, Any]:
    """The epic-level acceptance-verification oracle — one `{item, tier, satisfied, evidence}` disposition
    at the strongest tier available, so a finished epic records how its acceptance was judged. Judged
    uniformly against all coder children (one exclusion below)."""
    # A never-dispatched Finish-filed follow-up is excluded: `rollup_ready` requires all merged, and the
    # report computed at the finisher's settle would otherwise show an unsatisfied item for ever
    # (see `_is_undispatched_finish_follow_up`).
    coders = [
        c for c in children
        if c.persona in EPIC_BUILD_PERSONAS and not _is_undispatched_finish_follow_up(c)
    ]
    has_ui = _epic_has_ui_surface(epic, children)
    required_checks = sorted(
        {name for c in coders for name in _parse_required_checks(c.body)}
    )
    rollup_ready = bool(coders) and all(
        epics._child_rollup_ready(c.state, c.persona, c.outcome_pr_url, c.outcome_no_pr_reason)
        for c in coders
    )

    if has_ui:
        item = {
            "item": 1, "tier": "ui_screenshot", "satisfied": False,
            "evidence": "epic delivers a UI surface; no automated screenshot evidence captured",
        }
    elif required_checks:
        item = {
            "item": 1, "tier": "ac_named_check_green", "satisfied": rollup_ready,
            "evidence": (
                f"required check(s) {', '.join(required_checks)} named on coder "
                f"child(ren) {[c.id for c in coders]}; rollup_ready={rollup_ready}"
            ),
        }
    else:
        item = {
            "item": 1, "tier": "merge_completeness", "satisfied": rollup_ready,
            "evidence": f"{len(coders)} coder child(ren), rollup_ready={rollup_ready}",
        }
    return {"items": [item], "all_resolved": item["satisfied"]}


def execute_finish_phase(finish_id: int, session: Session) -> Optional[dict[str, Any]]:
    """The Finish execution engine: run the full verify/acceptance/metrics sweep for ONE finish child
    and flip it `→ done` idempotently, which lets the epic's own rollup proceed on the same request. Returns
    the persisted report, or None when `finish_id` is not a finish child with an epic parent. Safe to call
    repeatedly, which jaahd's crash backstop relies on: an already-`done` child returns its persisted
    report, and a still-active one recomputes it before the guarded UPDATE either wins or no-ops."""
    finish = session.get(Task, finish_id)
    if finish is None or finish.persona != PERSONA_FINISH:
        return None
    epic = session.get(Task, finish.parent_task_id) if finish.parent_task_id is not None else None
    if epic is None:
        return None
    if finish.state == DONE:
        return (epic.metrics or {}).get("finish_report")

    children = _finish_epic_children(epic.id, finish.id, session)
    verify = _verify_resolution_report(epic.id, children, session, org=epic.org_id)
    acceptance = _acceptance_check(epic, children)
    time_metrics = _epic_time_metrics(epic.id, children, session)
    token_metrics = _epic_token_metrics(children)
    benchmarks = _epic_efficiency_benchmarks(children, session)
    # Every PR the build children opened, in any repository, in child order.
    build_ids = [c.id for c in children if c.persona in EPIC_BUILD_PERSONAS]
    pr_map = task_prs.prs_by_task(session, build_ids)
    prs = [
        {"child_id": child_id, **pr.model_dump()}
        for child_id in build_ids for pr in _prs_out(pr_map, child_id)
    ]

    report = {
        "finish_task_id": finish.id,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "verify": verify,
        "acceptance": acceptance,
        "time": time_metrics,
        "tokens": token_metrics,
        "benchmarks": benchmarks,
        "prs": prs,
    }
    metrics = dict(epic.metrics or {})
    metrics["finish_report"] = report
    metrics["tokens_in"] = token_metrics["tokens_in"]
    metrics["tokens_out"] = token_metrics["tokens_out"]
    metrics["total_seconds"] = time_metrics["total_seconds"]
    session.execute(
        update(Task).where(Task.id == epic.id)
        .values(metrics=metrics, **actors.task_actor_values(session))
    )

    result = session.execute(
        update(Task)
        .where(Task.id == finish.id, Task.persona == PERSONA_FINISH, Task.state != DONE)
        .values(state=DONE, **dispatch._RUNNER_BINDING_CLEARED, **actors.task_actor_values(session))
    )
    if result.rowcount > 0:
        emit_console_event(session, entity="task", id=finish.id, action=DONE)
        # The finisher's own last word (a no-op when it filed a follow-up). Re-read `finish`: the guarded UPDATE
        # changed its `state` underneath, and the publisher checks `child.state != DONE`. Same transaction.
        session.refresh(finish)
        _publish_epic_final_report(finish, epic, session)
        record_task_note(
            session,
            epic.id,
            "finish_report",
            # The count only — the label already says "Finish report".
            f"{len(children)} child(ren) swept",
            extra={
                "finish_task_id": finish.id,
                # An unmet acceptance item withholds the clean-done signal exactly like an unresolved
                # verify finding, combined here so a consumer keying off `all_resolved` covers both.
                "all_resolved": verify["all_resolved"] and acceptance["all_resolved"],
            },
        )
    session.commit()
    if result.rowcount > 0:
        epics._try_complete_epic(epic.id, session)
    return report


def _live_follow_up(epic_id: int, dedup_key: str, session: Session) -> Optional[Task]:
    """The epic's live follow-up filed under `dedup_key`. Live rows only: matching an archived or
    `system_failed` row would lose the gap. `done` and `paused` do match."""
    return session.scalars(
        select(Task)
        .where(
            Task.parent_task_id == epic_id,
            Task.finish_follow_up_key == dedup_key,
            Task.archived_at.is_(None),
            Task.state.not_in(_FINISH_FOLLOW_UP_DEDUP_EXCLUDED_STATES),
        )
        .order_by(Task.id)
    ).first()


def append_finish_follow_up(
    finish_child: Task, proposal: dict[str, Any], session: Session
) -> Optional[dict[str, Any]]:
    """File ONE gap the terminal Finish phase found (the finisher's LAST SHOT rule) as a REGULAR, VISIBLE
    coder child of the same epic. Returns `{id, created, epic_id}`, or None when refused — not a
    `finish` child of an epic, an archived filer or epic, a filer outside
    `_APPEND_FINISH_FOLLOW_UP_STATES`, or a settled epic (the route 404s).

    An ordinary coder (`merged_pr`, the epic's priority), so no persona/dispatch/settle logic changes;
    `finish_follow_up_key` only groups it under Finish. It holds the epic `active` until its PR merges —
    intended. Dedup is fixed, not refreshing (unlike `create_system_task`): a re-file returns the
    existing id with `created=False` and writes nothing, matched on the stored `finish_follow_up_key`."""
    epic_id = finish_child.parent_task_id
    if finish_child.persona != PERSONA_FINISH or epic_id is None:
        return None
    # Row-lock the epic before any liveness check: the dedup SELECT and the INSERT are not atomic, and the
    # lock is also what makes "still live" meaningful. (A no-op on SQLite.)
    epic = session.get(Task, epic_id, with_for_update=True)
    if epic is None or epic.persona != PERSONA_EPIC:
        return None
    # Liveness after the lock, on re-read rows: `append_tasks` archives a `done` finish child while holding
    # this lock, so an earlier check would be stale.
    session.refresh(finish_child)
    # Archived filer: `_has_unmet_prerequisite` ignores archived prerequisites, so the follow-up would
    # dispatch immediately. Outside `_APPEND_FINISH_FOLLOW_UP_STATES` no finisher is driving it.
    if finish_child.archived_at is not None:
        return None
    if finish_child.state not in _APPEND_FINISH_FOLLOW_UP_STATES:
        return None
    # Archived/settled epic: nothing would roll the work up, and the row would dispatch under an epic that
    # reads `done` (the rollup paths are guarded `state == ACTIVE`).
    if epic.archived_at is not None or epic.state in _EPIC_SETTLED_STATES:
        return None

    dedup_key = proposal["dedup_key"]
    existing = _live_follow_up(epic.id, dedup_key, session)
    if existing is not None:
        return {"id": existing.id, "created": False, "epic_id": epic.id}
    # Resolve every sibling prerequisite before inserting: an unknown key is the filer's mistake, and a row
    # filed without its edge would dispatch early and fail on the gate its body describes.
    prerequisites = []
    for key in dict.fromkeys(proposal.get("depends_on") or ()):
        prereq = _live_follow_up(epic.id, key, session)
        if prereq is None:
            raise HTTPException(
                status_code=422,
                detail=f"depends_on: no follow-up with dedup_key {key!r} under epic {epic.id}; file it first",
            )
        prerequisites.append(prereq.id)

    # Same title cap as other auto-generated children, normalized never rejected (see `append_tasks`).
    title = short_title(proposal["title"], prose=False)
    if title != proposal["title"]:
        log.warning(
            "append_finish_follow_up: shortened follow-up title for epic %s: %r -> %r",
            epic.id, proposal["title"], title,
        )
    executor_id = executor_user_id(session, finish_child.id)
    row = Task(
        project_id=epic.project_id,
        parent_task_id=epic.id,
        source=task_source.source_for(epic, task_source.AGENT),
        is_plannable=False,
        persona=PERSONA_CODER,
        # READY, not DRAFT: the fan-out sweep enumerates only `active` epics, and the epic may be about to stop
        # being active; `ready` plus the dependency edge needs no sweep.
        state=READY,
        title=title,
        # Verbatim: the operator and the next runner both read this body; the dedup key has its own column.
        body=proposal["body"],
        priority=epic.priority,
        # `hidden` defaults false (visible on the board), the point of filing here. `phase_index`/`phase_name`
        # stay NULL so no cross-phase edge applies.
        finish_follow_up_key=dedup_key,
        # Owned by the finisher's executor; pushes as the epic's starter (see `append_tasks`).
        created_by_id=executor_id,
        assigned_to_id=epic.assigned_to_id,
    )
    session.add(row)
    session.flush()   # assign row.id for the dependency edge + the note/event payloads below

    # The follow-up depends on the finisher, plus any sibling follow-ups it named, so `dispatchable_tasks`
    # holds it until each is `done`. A fresh row cannot cycle; the guard is defensive.
    for prereq_id in (finish_child.id, *prerequisites):
        if _would_create_cycle(row.id, prereq_id, session):
            session.rollback()
            raise HTTPException(
                status_code=422,
                detail=(
                    f"follow-up {row.id} -> {prereq_id} would create a cycle "
                    "(unexpected for a fresh filing — aborting)"
                ),
            )
        session.add(TaskDependency(task_id=row.id, depends_on_task_id=prereq_id))
    emit_console_event(session, entity="task", id=row.id, action=READY)
    # On the epic, like `plan_fanout`: it explains why the epic outlived its finisher.
    record_task_note(
        session,
        epic.id,
        "finish_follow_up",
        # The label already names the kind of note this is.
        f"filed as coder child {row.id}",
        extra={
            "follow_up_task_id": row.id,
            "finish_task_id": finish_child.id,
            "dedup_key": dedup_key,
        },
    )
    session.commit()
    return {"id": row.id, "created": True, "epic_id": epic.id}
