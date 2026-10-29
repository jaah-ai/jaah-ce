"""The automation vocabulary — stdlib-only, so `app/models/`
renders its CHECKs from these tuples and revision 0417 can read them without the route layer.

An Automation (`models.Automation`) is one parent row — identity, owner, Target (a live rule) and Action (a
registry type plus params) — over exactly one When-detail child: a `crons` row (schedule) or a `task_triggers`
row (event). The behaviour lives in `app/automation_service.py` (writes, reads),
`app/target_resolver.py` (rules → targets), `app/automation_actions.py` (the action registry) and
`app/routes/automations.py` (the console API).
"""
from __future__ import annotations

AUTOMATIONS_TABLE = "automations"
AUTOMATION_RUN_TARGETS_TABLE = "automation_run_targets"

# ── When ─────────────────────────────────────────────────────────────────────────────────────────────
# Which child table holds the When detail: `crons` for a schedule, `task_triggers` for an event.
WHEN_SCHEDULE = "schedule"
WHEN_EVENT = "event"
WHEN_KINDS: tuple[str, ...] = (WHEN_SCHEDULE, WHEN_EVENT)

# ── Target (architecture §6.1) ───────────────────────────────────────────────────────────────────────
# A rule, never an expanded id list: `all` and `all_except` are resolved at each firing (D3), so a project
# created later is included without an edit.
TARGET_PROJECTS = "projects"
TARGET_NODES = "nodes"
TARGET_KINDS: tuple[str, ...] = (TARGET_PROJECTS, TARGET_NODES)

MODE_ALL = "all"
MODE_SPECIFIC = "specific"
MODE_ALL_EXCEPT = "all_except"
TARGET_MODES: tuple[str, ...] = (MODE_ALL, MODE_SPECIFIC, MODE_ALL_EXCEPT)

# ── Per-target outcome of one firing (`automation_run_targets.outcome`, §2.3; the fire engine writes it) ──
OUTCOME_FILED = "filed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_ERROR = "error"
OUTCOME_NOT_REACHED = "not_reached"
OUTCOME_QUEUED = "queued"
RUN_TARGET_OUTCOMES: tuple[str, ...] = (
    OUTCOME_FILED, OUTCOME_SKIPPED, OUTCOME_ERROR, OUTCOME_NOT_REACHED, OUTCOME_QUEUED,
)

# ── Status (§6.3): derived on read, never a column ────────────────────────────────────────────────────
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_FAILING = "failing"
STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_FAILING)

# ── Action types the core registers itself (`app/automation_actions.py`) ─────────────────────────────
ACTION_TASK = "task"
ACTION_NOOP = "noop"
ACTION_EMAIL = "email.send"

# The cron activity a schedule automation's Task action rides when the Runner bag can express it (its
# `crons.activity_params` hold the action for the child writers and agent verbs); the fire engine fires it whatever it
# rides (`automation_service.carrier_for`).
LEGACY_RUNNER_ACTIVITY = "runner"

