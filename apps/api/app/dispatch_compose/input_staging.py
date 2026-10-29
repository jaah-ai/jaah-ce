"""Where a runner's attachments and prerequisite input artifacts are staged on its node."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.task_manifest import INPUT_MANIFEST, KIND_MEDIA

log = logging.getLogger(__name__)


# Dispatch-time attachment materialization root: a host `/tmp` path outside any worktree, because
# `compose()` runs before a worktree is leased. Env-overridable for tests/alt hosts.
_ATTACHMENTS_TMP_ROOT = Path("/tmp/cockpit-task-attachments")


def _task_workdir(root: Path, console_task_id: object, what: str) -> Path:
    """`root/task-<id>`: a per-dispatch dir keyed by task id. Task-keyed, not slot-keyed (a reused slot would
    leak a prior tenant's files). The key must be a plain non-negative integer, since `shutil.rmtree` follows."""
    # `[0-9]+`, NOT `\d+`: `\d` admits every Unicode decimal digit.
    if console_task_id is not None and re.fullmatch(r"[0-9]+", str(console_task_id)):
        return root / f"task-{console_task_id}"
    raise ValueError(f"{what} workdir needs a plain non-negative integer task id, got {console_task_id!r}")


def _attachment_workdir(console_task_id: object) -> Path:
    """The per-dispatch attachment materialization dir; the node wipes it before every (re)dispatch."""
    return _task_workdir(_ATTACHMENTS_TMP_ROOT, console_task_id, "attachment")


# ── Prerequisite-artifact materialization (decision D5) ──
# Staging root for a dependent stage's inputs (prerequisites' settled `prd`/`architecture` documents).
# Separate from `_ATTACHMENTS_TMP_ROOT`: attachment staging is best-effort, a required input is
# fail-loud, and one directory for both would let the weaker posture win.
_TASK_INPUTS_TMP_ROOT = Path("/tmp/cockpit-task-inputs")


def _task_inputs_workdir(console_task_id: object) -> Path:
    """The per-dispatch prerequisite-input dir."""
    return _task_workdir(_TASK_INPUTS_TMP_ROOT, console_task_id, "prerequisite-input")


def materialized_input_name(kind: str) -> str:
    """The basename a prerequisite artifact of `kind` is written under (`prd.md`, `architecture.md`).

    Derived from the kind, never the uploaded filename: it renders above the untrusted-content fence, and a
    filename is runner-authored. Extension from `KIND_MEDIA`'s first accepted suffix."""
    return f"{kind}{KIND_MEDIA[kind][0][0]}"


def _expected_inputs(persona: str | None, stage: dict | None) -> tuple[str, ...]:
    """The prerequisite-input kinds a task receives (D5): a playbook stage's `receives` (resolved by the engine),
    else `task_manifest.INPUT_MANIFEST` for its persona. No manifest row (every build persona) yields nothing."""
    return tuple(stage.get("receives") or ()) if stage else (INPUT_MANIFEST.get(persona) or ())


def stage_input_artifacts(
    prerequisite_artifacts: list, console_task_id: object, persona: str | None, stage: dict | None
) -> tuple[list[dict], str | None]:
    """The prerequisite-input handoff as the node stages it: `(records, directory)`. The ONE home both launch
    builders call — `compose` for a model runner and `fleet_verdicts.agent_launch` for an SDK agent — so a
    pipeline stage an agent runs gets its `prd`/`architecture` exactly as a model would. `prerequisite_artifacts`
    are `routes/tasks/task_wire.py::_task_prerequisite_artifacts`' rows (latest-wins, Reset-aware)."""
    return _input_artifact_staging(prerequisite_artifacts, console_task_id, _expected_inputs(persona, stage))


def _input_artifact_staging(
    prerequisite_artifacts: list, console_task_id: object, expected_inputs: tuple[str, ...]
) -> tuple[list[dict], str | None]:
    """Turn the projection's `prerequisite_artifacts` rows into the node's staging instruction plus the
    directory holding them; `([], None)` when there is nothing to stage.

    Path computed here, bytes written on the node; each record carries the artifact's own `task_id`, row
    id, kind and basename. `expected_inputs` has no default (empty = nothing staged). A row is dropped
    when its kind is not expected, unknown to `KIND_MEDIA`, or zero bytes (permanent; renders NOT
    AVAILABLE). The directory is returned even when no row survives, so the node wipes a previous
    attempt's document. An unkeyable unit stages nothing, silently."""
    if not expected_inputs:
        return [], None
    try:
        workdir = _task_inputs_workdir(console_task_id)
    except ValueError as exc:
        log.warning("no prerequisite-input workdir for task %s (%s) — staging no input artifacts",
                    console_task_id, exc)
        return [], None
    records = []
    for row in prerequisite_artifacts:
        kind = row.get("kind")
        if kind not in KIND_MEDIA or kind not in expected_inputs:
            continue
        if not row.get("size_bytes"):
            log.warning("prerequisite %s artifact %s of task %s is a zero-byte row — staging nothing for "
                        "this kind; the prompt renders it NOT AVAILABLE",
                        kind, row.get("id"), row.get("task_id"))
            continue
        records.append({
            "id": row["id"],
            "task_id": row["task_id"],
            "kind": kind,
            "filename": materialized_input_name(kind),
        })
    return records, str(workdir)
