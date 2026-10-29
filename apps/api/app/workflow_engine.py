"""Read layer over DBOS for the workflows in `app/workflow_registry.py`.

  · Goes through `DBOS.list_workflows`, not SQL: `durable_dbos_sys` is the library's own schema.
  · Nothing of the library's crosses the wire; `WorkflowRun` is the flat value the route renders.
  · Never raises into the page: an unreachable or unlaunched DBOS (every API test) yields an empty
    answer, and the route reports `engine_available` so empty is not read as "nothing ran".

Read-only by design: no cancel, retry, fork or resume.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

# Public in `dbos` and importable without a launched runtime, so the route can validate `status` anywhere.
# Not mirrored: the library owns this closed set.
from dbos import WorkflowStatusString

log = logging.getLogger("cockpit-workflows")

#: DBOS's own status vocabulary, read off the library rather than spelled.
STATUSES: tuple[str, ...] = tuple(member.value for member in WorkflowStatusString)

# How much of an input, output or error reaches the browser: arbitrary payloads, so bounded (an
# `authenticate_account` reply carries a node's whole stdout). 500 fits `task_done`'s status map. The
# error field is under the same cap (see `_error_text`).
PAYLOAD_MAX_CHARS = 500

# How many recent runs the per-type counts fold from: a window, not a lifetime total (the route tells the
# UI). `list_workflows` has no count-only call and DBOS prunes on its own.
AGGREGATE_SCAN_LIMIT = 500


@dataclass(frozen=True)
class WorkflowRun:
    """One workflow run, narrowed to what the runs page renders."""

    workflow_id: str
    #: The DBOS function name — the join back to `workflow_registry`, where the label lives.
    name: str
    status: str
    queue_name: Optional[str]
    executor_id: Optional[str]
    app_version: Optional[str]
    recovery_attempts: Optional[int]
    # Created/enqueued, dequeued by a worker (None while queued), completed (None while running).
    created_at: Optional[datetime]
    dequeued_at: Optional[datetime]
    completed_at: Optional[datetime]
    error: Optional[str]
    #: Arguments and return value, rendered and bounded (`PAYLOAD_MAX_CHARS`). Strings because a dozen
    #: unrelated workflows share no shape.
    input: Optional[str]
    output: Optional[str]


@dataclass(frozen=True)
class RunAggregate:
    """The per-type rollup over the scan window — see `aggregate` for what the window means."""

    runs: int
    by_status: dict[str, int]
    last_run_at: Optional[datetime]
    last_status: Optional[str]


#: The rollup for a never-run workflow, also substituted when the engine could not be asked.
EMPTY_AGGREGATE = RunAggregate(runs=0, by_status={}, last_run_at=None, last_status=None)


def _instant(epoch_ms: Any) -> Optional[datetime]:
    """A DBOS epoch-millisecond field as an aware UTC datetime, or None.

    Tolerant: a bad field costs one timestamp, not the page.
    """
    if epoch_ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _error_text(error: Any) -> Optional[str]:
    """The workflow's error as a bounded string, whatever DBOS handed back.

    DBOS returns either a deserialized exception or raw serialized text; both are rendered. Delegates to
    `_payload` because errors are the field most likely to be enormous (they quote the payload, and a
    `StatementError` appends statement and parameters). Truncation is marked.
    """
    return _payload(error)


def _payload(value: Any) -> Optional[str]:
    """An input or an output as a bounded display string, or None when there is nothing to show.

    `repr`, not `json.dumps`, which would raise on a non-serializable object. Truncation is marked.
    """
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else repr(value)
    except Exception:  # noqa: BLE001 — a __repr__ that raises must not cost the row
        return "<unrenderable>"
    if not text:
        return None
    if len(text) <= PAYLOAD_MAX_CHARS:
        return text
    return f"{text[:PAYLOAD_MAX_CHARS]}… (truncated at {PAYLOAD_MAX_CHARS} characters)"


def _run(status: Any) -> WorkflowRun:
    """One `WorkflowStatus` narrowed to the fields the page renders."""
    return WorkflowRun(
        workflow_id=str(getattr(status, "workflow_id", "")),
        name=str(getattr(status, "name", "") or ""),
        status=str(getattr(status, "status", "") or ""),
        queue_name=getattr(status, "queue_name", None),
        executor_id=getattr(status, "executor_id", None),
        app_version=getattr(status, "app_version", None),
        recovery_attempts=getattr(status, "recovery_attempts", None),
        created_at=_instant(getattr(status, "created_at", None)),
        dequeued_at=_instant(getattr(status, "dequeued_at", None)),
        completed_at=_instant(getattr(status, "completed_at", None)),
        error=_error_text(getattr(status, "error", None)),
        input=_payload(getattr(status, "input", None)),
        output=_payload(getattr(status, "output", None)),
    )


def list_runs(
    *,
    names: Sequence[str],
    status: Optional[str] = None,
    workflow_id_prefix: Optional[str] = None,
    workflow_ids: Optional[Sequence[str]] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int,
    offset: int = 0,
) -> list[WorkflowRun]:
    """One page of runs across `names`, newest first. `[]` when DBOS cannot answer.

    `names` is the exclusion (no "everything but" filter), keeping cron ticks off the page; the route
    refuses unregistered names. `start_time`/`end_time` are ISO 8601 strings passed through as-is; the route
    pre-validates them because a malformed one would raise inside DBOS and be swallowed into `[]` below
    (`app/routes/workflows.py`).
    """
    try:
        from dbos import DBOS

        return [
            _run(row)
            for row in DBOS.list_workflows(
                name=list(names),
                status=status,
                workflow_id_prefix=workflow_id_prefix,
                workflow_ids=list(workflow_ids) if workflow_ids is not None else None,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
                sort_desc=True,
                load_input=True,
                # DBOS gates the error field on `load_output`, so it must be True here.
                load_output=True,
            )
        ]
    except Exception as exc:  # noqa: BLE001 — read-only operator page; see the module docstring
        # Debug, not warning: the common cause is a process with no DBOS runtime (the test suite).
        log.debug("workflow runs unavailable (%r)", exc)
        return []


def aggregate(*, names: Sequence[str], scan_limit: int = AGGREGATE_SCAN_LIMIT) -> dict[str, RunAggregate]:
    """Per-name counts folded from the most recent `scan_limit` runs across all of `names`.

    A bounded window, not a lifetime total; the route carries `scan_limit` to the UI. The window bounds
    rows read, not rows per name, so the same workflow's count differs between an all-names call and a
    one-name call; `app/routes/workflows.py` owns which screen reads which.

    One `list_workflows` call for every name, with payloads not loaded.

    Returns a full zero-filled map on success and `{}` on failure, so a caller can tell "no runs" from
    "engine did not answer".
    """
    try:
        from dbos import DBOS

        rows = DBOS.list_workflows(
            name=list(names),
            limit=scan_limit,
            sort_desc=True,
            load_input=False,
            load_output=False,
        )
    except Exception as exc:  # noqa: BLE001 — read-only operator page; see the module docstring
        log.debug("workflow aggregate unavailable (%r)", exc)
        return {}

    counts: dict[str, dict[str, int]] = {name: {} for name in names}
    newest: dict[str, tuple[Optional[datetime], str]] = {}
    for row in rows:
        name = str(getattr(row, "name", "") or "")
        if name not in counts:
            # Unreachable through the filter; dropped so a looser future filter cannot grow the map.
            continue
        state = str(getattr(row, "status", "") or "")
        counts[name][state] = counts[name].get(state, 0) + 1
        # `sort_desc=True`: the first row per name is newest, with no comparison (`created_at` may be None).
        newest.setdefault(name, (_instant(getattr(row, "created_at", None)), state))

    return {
        name: RunAggregate(
            runs=sum(counts[name].values()),
            by_status=counts[name],
            last_run_at=newest[name][0] if name in newest else None,
            last_status=newest[name][1] if name in newest else None,
        )
        for name in names
    }


def engine_reachable() -> bool:
    """Whether a DBOS runtime is answering at all.

    Only asked when a page came back empty, since `list_runs` returns `[]` for both "no match" and
    "no engine".
    """
    try:
        from dbos import DBOS

        DBOS.list_workflows(limit=1, load_input=False, load_output=False)
        return True
    except Exception as exc:  # noqa: BLE001 — read-only operator page; see the module docstring
        log.debug("workflow engine unreachable (%r)", exc)
        return False

