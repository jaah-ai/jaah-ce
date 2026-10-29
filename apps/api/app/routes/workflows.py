"""The workflow REST boundary: a read-only window onto every non-cron DBOS workflow and its runs.

`cron_tick` is excluded; it has its own `cron_runs` ledger (`app/workflow_registry.py` says why). Its PENDING
runs alone have a route (`list_pending_cron_ticks`): a tick abandoned by its executor never reaches that ledger.
The catalogue is curated (`app/workflow_registry.py`: name, label, description, trigger), so a type
is listed before it ever runs; the runs are live (`app/workflow_engine.py` owns how DBOS is asked).

Two windows: the list folds counts from the newest `AGGREGATE_SCAN_LIMIT` runs shared by all
workflows, a type's own page from that workflow's own (see `WorkflowDetailOut`). No number is
rendered without its window (`web/src/pages/workflows/vocabulary.ts::windowCaption`).

Read-only on purpose: no cancel/retry/fork/resume, since each needs authority and audit decisions.
An unreachable engine is reported via `engine_available`, not a 500 or an empty list.

Superadmin, twice: `Depends(require_superadmin)` plus `@superadmin_only` (what `authz_guard` reads;
`test_authz_registry.py` asserts completeness), as in `app/routes/crons.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import require_superadmin
from app.authz.catalog import Tenancy
from app.authz.declarations import superadmin_only
from app.entity_shape import from_row
from app.models.identity import AdminUser
from app.routes.crons import _reject
from app.workflow_engine import (
    AGGREGATE_SCAN_LIMIT,
    EMPTY_AGGREGATE,
    STATUSES,
    RunAggregate,
    WorkflowRun,
    aggregate,
    engine_reachable,
    list_runs,
)
from app.workflow_registry import CRON_TICK_WORKFLOW, WorkflowDescriptor, get, listed, listed_names

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

# The runs page's default and ceiling, as in `app/routes/crons.py`: the engine table grows unbounded.
RUNS_DEFAULT_LIMIT = 50
RUNS_MAX_LIMIT = 200

# How many PENDING cron ticks one read answers: a healthy plane holds a handful at most, so a longer list is
# already the finding.
PENDING_TICKS_LIMIT = 50

# Offset paging scans proportionally to the offset; past a few pages the answer is a filter.
# Refused rather than clamped.
OFFSET_MAX = 5_000

# The field is a workflow-id prefix and the longest minted id is under a hundred characters, so a
# longer term is a caller error; refused here where the message names the field.
SEARCH_MAX_LENGTH = 200


# ── Wire models ───────────────────────────────────────────────────────────────────────────────────────


class WorkflowTypeOut(BaseModel):
    """One workflow type: the curated identity, plus what the engine has seen of it lately.

    Flattened: the identity is always present; the counts are a bounded window that can be zero because
    nothing ran or because the engine did not answer (`engine_available` on the payload says which).
    """

    name: str
    label: str
    description: str
    trigger: str
    #: The DBOS queue, or None for a workflow started directly (see `WorkflowDescriptor.queue`).
    queue: Optional[str]
    #: Runs within the scan window, not a lifetime total; `window` on the payload is its size.
    runs: int
    #: `{DBOS status: count}` within the same window. Sparse: a status with no runs is absent.
    by_status: dict[str, int]
    last_run_at: Optional[datetime]
    last_status: Optional[str]


class WorkflowTypesOut(BaseModel):
    """Every registered non-cron workflow, whether or not it has ever run."""

    workflows: list[WorkflowTypeOut]
    #: How many recent runs the counts were folded from, one window shared by all workflows, so
    #: the UI can say "of the last N runs across all workflows".
    window: int
    #: False when DBOS could not be reached; otherwise an outage and an idle CP render identically.
    engine_available: bool


class WorkflowDetailOut(WorkflowTypeOut):
    """One type's own page. Extends the list row, and both are projected by `_type_out`, so no field
    is on one screen and not the other.

    The window differs on purpose: this page folds the newest `AGGREGATE_SCAN_LIMIT` runs of this
    workflow alone, the better measurement for one workflow and the same population as the runs table
    beneath it. So `runs` here and on the list row are two true numbers; every surface must name its
    window (`vocabulary.ts::windowCaption` requires the scope).
    """

    #: This workflow's own window (the scan limit, not a row count), so the wording names the population.
    window: int
    engine_available: bool


class WorkflowRunOut(BaseModel):
    """One run as the page renders it. `app/workflow_engine.py` is the single home for where each field
    comes from and why the payloads are bounded."""

    workflow_id: str
    #: Both travel: the label is what an operator reads, the name what the `name` filter accepts.
    name: str
    label: str
    #: DBOS's own closed set as a string, so a newer DBOS state reaches the page as itself.
    status: str
    queue_name: Optional[str]
    executor_id: Optional[str]
    app_version: Optional[str]
    #: 1 for a run that ran once; above 1 means it was recovered by another executor.
    recovery_attempts: Optional[int]
    created_at: Optional[datetime]
    dequeued_at: Optional[datetime]
    completed_at: Optional[datetime]
    error: Optional[str]
    input: Optional[str]
    output: Optional[str]


class WorkflowRunsPage(BaseModel):
    """One page of runs across every registered workflow, newest first.

    `next_offset` is the following page's offset, or None at the end; no total, because DBOS has no
    count-only call.
    """

    runs: list[WorkflowRunOut]
    limit: int
    offset: int
    next_offset: Optional[int]
    engine_available: bool


# ── Projection ────────────────────────────────────────────────────────────────────────────────────────


def _type_out(descriptor: WorkflowDescriptor, rollup: RunAggregate) -> dict[str, Any]:
    """The pinned type fields as a dict: the one producer for both the list row and the detail."""
    return {
        "name": descriptor.name,
        "label": descriptor.label,
        "description": descriptor.description,
        "trigger": descriptor.trigger,
        "queue": descriptor.queue,
        "runs": rollup.runs,
        "by_status": rollup.by_status,
        "last_run_at": rollup.last_run_at,
        "last_status": rollup.last_status,
    }


def _run_out(run: WorkflowRun) -> WorkflowRunOut:
    descriptor = get(run.name)
    return from_row(
        WorkflowRunOut, run,
        # The raw name when the registry does not carry it (unreachable via the filter), as
        # `crons.py::_activity_label` does.
        label=descriptor.label if descriptor is not None else run.name,
    )


# ── The type catalogue ────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=WorkflowTypesOut)
@superadmin_only(Tenancy.OPERATOR)
def list_workflow_types(
    _actor: AdminUser = Depends(require_superadmin),
) -> WorkflowTypesOut:
    """Every registered non-cron workflow, with its recent-run rollup.

    Every registry entry appears, with zero counts if it never ran ("shipped but never fired" is
    actionable). One engine call for all types (`workflow_engine.aggregate`).
    """
    rollups = aggregate(names=listed_names())
    return WorkflowTypesOut(
        workflows=[
            WorkflowTypeOut(**_type_out(descriptor, rollups.get(descriptor.name, EMPTY_AGGREGATE)))
            for descriptor in listed()
        ],
        window=AGGREGATE_SCAN_LIMIT,
        # An empty map is the engine's "could not answer" (`workflow_engine.aggregate` owns that contract).
        engine_available=bool(rollups),
    )


# ── The runs window ───────────────────────────────────────────────────────────────────────────────────
# Declared before `/{name}`: FastAPI matches in order, and the path parameter would swallow `/runs`.


@router.get("/runs", response_model=WorkflowRunsPage)
@superadmin_only(Tenancy.OPERATOR)
def list_workflow_runs(
    name: Optional[str] = Query(
        default=None,
        description=(
            "Restrict to one registered workflow. A name this catalogue does not carry is refused — "
            "which is also what keeps cron ticks off this page, since DBOS offers no exclusion filter "
            "and the only way to say 'everything but cron_tick' is to name what we want."
        ),
    ),
    status: Optional[str] = Query(
        default=None, description="One DBOS workflow status, e.g. SUCCESS, ERROR, ENQUEUED."
    ),
    search: Optional[str] = Query(
        default=None,
        max_length=SEARCH_MAX_LENGTH,
        description=(
            "Matched as a workflow-id PREFIX, not a substring: DBOS indexes ids by prefix and offers no "
            "contains filter, so a fragment from the middle of an id will not match. The term reaches "
            "the engine as a bound `LIKE` parameter that DBOS does not escape, so SQL's own wildcards "
            "(`%`, `_`) are LIVE here and widen the prefix — harmless (the registry allowlist still "
            "bounds which workflows can be returned) but worth knowing before reading a surprising "
            "result. No registered workflow's ids contain either character."
        ),
    ),
    start_time: Optional[str] = Query(
        default=None,
        description=(
            "ISO 8601 instant; runs created at or after it. A value that will not parse is refused — "
            "unrefused it would reach the engine, raise there, and render as an empty page."
        ),
    ),
    end_time: Optional[str] = Query(
        default=None,
        description=(
            "ISO 8601 instant; runs created at or before it. Refused when malformed, like `start_time`."
        ),
    ),
    limit: int = Query(default=RUNS_DEFAULT_LIMIT, ge=1, le=RUNS_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=OFFSET_MAX),
    _actor: AdminUser = Depends(require_superadmin),
) -> WorkflowRunsPage:
    """Runs across every registered workflow, newest first, one bounded page at a time.

    Offset, not keyset: the ordering column and its index are DBOS's, and a cursor would couple to
    another library's schema. The page-shift seam is cheap at these volumes.

    `limit` and `offset` are capped by the signature and refused past it, not clamped silently.
    """
    if name is not None and get(name) is None:
        _reject("name", f"{name!r} is not a registered workflow; expected one of {list(listed_names())}")
    if status is not None and status not in STATUSES:
        _reject("status", f"{status!r} is not a DBOS workflow status; expected one of {list(STATUSES)}")
    # Parsed to refuse, not to substitute; DBOS still receives the original string. Unvalidated, DBOS
    # raises before any round trip, `list_runs` swallows it into `[]`, and `engine_reachable` answers True,
    # so a malformed filter would render as a healthy empty page. `fromisoformat` so we accept exactly
    # what DBOS accepts.
    for field, value in (("start_time", start_time), ("end_time", end_time)):
        if value is None:
            continue
        try:
            datetime.fromisoformat(value)
        except (TypeError, ValueError):
            _reject(field, f"{value!r} is not an ISO 8601 instant, e.g. 2026-01-01T00:00:00Z")

    # One row more than asked for, discarded: answers "is there another page?" without a count.
    runs = list_runs(
        names=[name] if name is not None else listed_names(),
        status=status,
        workflow_id_prefix=search or None,
        start_time=start_time,
        end_time=end_time,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(runs) > limit
    page = runs[:limit]
    return WorkflowRunsPage(
        runs=[_run_out(run) for run in page],
        limit=limit,
        offset=offset,
        # Never past the cap: a cursor the next request would refuse is worse than ending pagination.
        next_offset=(offset + limit) if has_more and offset + limit <= OFFSET_MAX else None,
        # A non-empty page is its own proof; the probe runs only when the page is ambiguous.
        engine_available=bool(page) or engine_reachable(),
    )


@router.get("/runs/{workflow_id:path}", response_model=WorkflowRunOut)
@superadmin_only(Tenancy.OPERATOR)
def get_workflow_run(
    workflow_id: str,
    _actor: AdminUser = Depends(require_superadmin),
) -> WorkflowRunOut:
    """One run by its exact id, in the runs page's row shape — so a pasted run URL opens the same record.

    Bounded to the registered workflows as the page is, so a cron tick's id is a 404 here too. A 404 also when
    the engine cannot answer: `list_runs` does not tell "no such run" from "no engine"."""
    runs = list_runs(names=listed_names(), workflow_ids=[workflow_id], limit=1)
    if not runs:
        raise HTTPException(status_code=404, detail=f"no workflow run with id {workflow_id!r}")
    return _run_out(runs[0])


@router.get("/cron-ticks/pending", response_model=WorkflowRunsPage)
@superadmin_only(Tenancy.OPERATOR)
def list_pending_cron_ticks(
    _actor: AdminUser = Depends(require_superadmin),
) -> WorkflowRunsPage:
    """The PENDING `cron_tick` runs (`sched-cron:*` ids), newest first, with the executor holding each.

    The one window onto cron ticks: the runs page excludes them (the module docstring says why), which also
    hid a tick whose executor died mid-run. Not paged: `PENDING_TICKS_LIMIT` bounds it.
    """
    runs = list_runs(names=(CRON_TICK_WORKFLOW,), status="PENDING", limit=PENDING_TICKS_LIMIT)
    return WorkflowRunsPage(
        runs=[_run_out(run) for run in runs],
        limit=PENDING_TICKS_LIMIT,
        offset=0,
        next_offset=None,
        engine_available=bool(runs) or engine_reachable(),
    )


# ── One type ──────────────────────────────────────────────────────────────────────────────────────────


@router.get("/{name}", response_model=WorkflowDetailOut)
@superadmin_only(Tenancy.OPERATOR)
def get_workflow_type(
    name: str,
    _actor: AdminUser = Depends(require_superadmin),
) -> WorkflowDetailOut:
    """One workflow's descriptor and its own rollup, or a 404.

    A 404, not an empty descriptor: an unregistered name is not a workflow with no runs. Its rollup uses
    its own window, not the list's (see `WorkflowDetailOut`).
    """
    descriptor = get(name)
    if descriptor is None:
        raise HTTPException(
            status_code=404,
            detail=f"no workflow named {name!r}; this catalogue carries {list(listed_names())}",
        )
    # This workflow's own window, not its share of the shared one (see `WorkflowDetailOut`).
    rollups = aggregate(names=(descriptor.name,))
    return WorkflowDetailOut(
        **_type_out(descriptor, rollups.get(descriptor.name, EMPTY_AGGREGATE)),
        window=AGGREGATE_SCAN_LIMIT,
        engine_available=bool(rollups),
    )
