"""The Runner activity — the carrier a Task automation's schedule child names (`app/automation_service.py`).

It carries params only: its schema is the Cron edit page's and the agent cron verbs' (`routes/agent_crons.py`), and
`check_project_keys` holds a bag to its shape. Every firing goes through the fire engine (`app/automation_fire.py`):
`cron_runtime._adopted_parent` gives a cron written without a parent its automation first, so `invoke` is reached
only by a row no writer produces (a system-flagged Runner) and refuses it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.cron_activities import (
    ActivityContext,
    ActivityDescriptor,
    ActivityResult,
    DefaultSchedule,
    ParamsError,
    register,
)
from app.crons import SCHEDULE_KIND_CRON
from app.model_catalog import INHERIT, MODEL_IDENTIFIERS
from app.persona_registry import SCHEDULABLE_PERSONAS
from app.routes.tasks.policy import PERSONA_CODER

ACTIVITY_TYPE = "runner"

# Capped: stored in `crons.activity_params` and re-read on every firing; enforced by `validate_params`.
MAX_INSTRUCTIONS_CHARS = 20_000

# The most projects one Runner cron may target — each is one task per firing.
MAX_PROJECTS = 20


def _runner_params() -> dict[str, Any]:
    """The `params` JSON Schema for a Runner cron.

    Both closed sets are imported, never restated: personas from `SCHEDULABLE_PERSONAS`, models from
    `MODEL_IDENTIFIERS` plus the `INHERIT` sentinel ("do not pin one").
    """
    return {
        "type": "object",
        "properties": {
            "instructions": {
                "type": "string",
                "title": "Instructions",
                "description": "Becomes the task body and prompt.",
                # Counts characters, so `"   "` passes; the fire engine strips and refuses a blank.
                "minLength": 1,
                "maxLength": MAX_INSTRUCTIONS_CHARS,
            },
            # The project's row id, never its name: a rename would silently sever the reference.
            # `minimum` is enforced by `_check_one_param`; existence is checked at firing time.
            "project_ids": {
                "type": "array",
                "title": "Projects",
                "description": "One task per project, each firing.",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                # Caps tasks per firing so a runaway list cannot saturate the worktree pool.
                "maxItems": MAX_PROJECTS,
                "uniqueItems": True,
            },
            "persona": {
                "type": "string",
                "title": "Persona",
                "description": "Runner role the task dispatches as.",
                "enum": list(SCHEDULABLE_PERSONAS),
                "default": PERSONA_CODER,
            },
            "model": {
                "type": "string",
                "title": "Model",
                "description": "Empty inherits the persona/project default.",
                "enum": [INHERIT, *MODEL_IDENTIFIERS],
                "default": INHERIT,
            },
            # Keeps the firing's task off the default board (`models/tasks.py:Task.hidden`); dispatch never
            # reads it. Default false so existing crons (no key stored) behave as before. Rendered as a
            # checkbox from the schema alone.
            # reaches the Cron edit page as the asked-for tick box with nothing typed on that side.
            "hidden": {
                "type": "boolean",
                "title": "Hidden task",
                "description": "Keep tasks off the default Tasks view.",
                "default": False,
            },
            # Full auto: the schedule's one question about humans, written to `autonomous` exactly as a
            # hand-filed task's Full Auto (one flag, one code path). Default true, matching every existing cron.
            "autonomous": {
                "type": "boolean",
                "title": "Full auto",
                "description": "Runner decides everything; asks only for what only a human can supply.",
                "default": True,
            },
        },
        # `project_ids` is required by `check_project_keys`, which names the key in its refusal.
        "required": ["instructions", "persona"],
        "additionalProperties": False,
    }


def configured_project_ids(params: Any) -> list[int]:
    """The projects a params bag names (`project_ids`); [] for none.

    Lenient on purpose (non-ints dropped, `bool` never read as an id): the route guards call it on any
    activity's bag, and `check_project_keys` is where a runner bag is held to the shape."""
    if not isinstance(params, dict):
        return []
    raw = params.get("project_ids")
    ids = raw if isinstance(raw, list) else []
    return [pid for pid in ids if isinstance(pid, int) and not isinstance(pid, bool)]


def check_project_keys(params: Any) -> list[int]:
    """The configured project ids, or `ParamsError` for a bag naming no project at all."""
    ids = configured_project_ids(params)
    if not ids:
        raise ParamsError(f"activity {ACTIVITY_TYPE!r} requires parameter 'project_ids'")
    return ids


def invoke(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """Reached only by a Runner row the engine does not fire (`_adopted_parent` skips a system row and a bag naming
    no project): refuse it, naming the missing key first."""
    check_project_keys(context.params)
    raise RuntimeError(f"cron {context.cron_id}: a Runner cron fires through its automation, and this one has none")


def _descriptor() -> ActivityDescriptor:
    return ActivityDescriptor(
        type=ACTIVITY_TYPE,
        label="Runner",
        description="Files one task per project per firing.",
        # Operator work, and the picker's headline offer.
        is_system=False,
        creatable=True,
        params=_runner_params(),
        # Daily at 09:00 in the control plane's zone.
        default_schedule=DefaultSchedule(kind=SCHEDULE_KIND_CRON, cron_expression="0 9 * * *"),
        invoke=invoke,
        # Covers the firing's own DB work (title LLM call is in the durable queue).
        timeout_seconds=120.0,
        # A floor makes it wakeable (`fire_now_sync` silently refuses a `None` min-gap) and coalesces
        # double-presses. Accepted side effect: `already_running` skips no longer feed the wedge
        # alarm; a hung firing still surfaces via the 120s timeout.
        min_gap_seconds=10.0,
    )


RUNNER_ACTIVITY: ActivityDescriptor = register(_descriptor())
