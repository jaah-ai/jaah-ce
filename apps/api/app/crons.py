"""The cron feature's closed vocabularies: the words the schema, the ORM and every cron child spell.

A leaf module (no `app` import at module scope) so a migration can read these tuples without the
route layer; the DDL and `models.py`'s constraint mirror are both rendered from these names.

Expressions and next-fire arithmetic live in `app/cron_schedule.py`; the activity catalogue in
`app/cron_activities.py`. There is deliberately no timezone vocabulary.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - the annotation's only reader
    from uuid import UUID

# ── The schedule discriminator ────────────────────────────────────────────────────────────────────────
# A row declares its schedule one way; `ck_crons_exactly_one_schedule` ties the kind to its column.
SCHEDULE_KIND_CRON = "cron"
SCHEDULE_KIND_INTERVAL = "interval"
SCHEDULE_KINDS: tuple[str, ...] = (SCHEDULE_KIND_CRON, SCHEDULE_KIND_INTERVAL)

# ── What one firing came to ───────────────────────────────────────────────────────────────────────────
# `acted`/`noop` are the activity's answer; `skipped`/`missed`/`error` are the wrapper's. `noop` stays
# distinct from `acted` so "is this cron doing work?" is answerable from the ledger.
RUN_OUTCOME_ACTED = "acted"
RUN_OUTCOME_NOOP = "noop"
RUN_OUTCOME_SKIPPED = "skipped"
RUN_OUTCOME_MISSED = "missed"
RUN_OUTCOME_ERROR = "error"
RUN_OUTCOMES: tuple[str, ...] = (
    RUN_OUTCOME_ACTED,
    RUN_OUTCOME_NOOP,
    RUN_OUTCOME_SKIPPED,
    RUN_OUTCOME_MISSED,
    RUN_OUTCOME_ERROR,
)

# The subset an activity may return; `ActivityResult` refuses the wrapper's three.
ACTIVITY_RETURNABLE_OUTCOMES: tuple[str, ...] = (RUN_OUTCOME_ACTED, RUN_OUTCOME_NOOP)

# ── How much of the ledger to keep ────────────────────────────────────────────────────────────────────
# Per-cron, because the right answer differs by cadence. `all` is the default so a new cron's first
# firings are debuggable.
RETENTION_NONE = "none"
RETENTION_ACTED = "acted"
RETENTION_ALL = "all"
RETENTION_MODES: tuple[str, ...] = (RETENTION_NONE, RETENTION_ACTED, RETENTION_ALL)
DEFAULT_RETENTION_MODE = RETENTION_ALL

def in_domain_sql(column: str, values: tuple[str, ...]) -> str:
    """A CHECK body pinning one column to a closed vocabulary: `<column> IN ('a', 'b')`.

    Single-homed so the revision and `models.py` agree on quoting (and a one-value tuple's `('x',)`
    repr is not legal SQL). A DDL renderer: `column` is interpolated bare, so the guards refuse anything
    that looks like request data being turned into a query.
    """
    if not column.isidentifier():
        raise ValueError(f"{column!r} is not a bare column identifier; this renders DDL, not a filter")
    if not values or not all(isinstance(v, str) and v.isidentifier() for v in values):
        raise ValueError("in_domain_sql takes a non-empty tuple of bare vocabulary constants")
    # Defence in depth; the guard above already refuses any value carrying an apostrophe.
    rendered = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{column} IN ({rendered})"


# ── Bounds the DATABASE holds ─────────────────────────────────────────────────────────────────────────
# The parser is linear in length, so an uncapped column lets one row cost seconds of CPU per tick.
# Here because the schema renders a CHECK from it: the database must refuse it too, not only the app.
MAX_CRON_EXPRESSION_CHARS = 256


# ── Table names ───────────────────────────────────────────────────────────────────────────────────────
# Named here so the revision, the ORM and a test assert against one spelling.
CRONS_TABLE = "crons"
CRON_RUNS_TABLE = "cron_runs"


# ── Which org a cron row belongs to ───────────────────────────────────────────────────────────────────


def org_for(*, is_system: bool, author_org: "UUID | None") -> "UUID | None":
    """The org a cron row belongs to: org zero for machinery, the author's own for a tenant's cron.

    System activities are fleet-wide sweeps whose rows stay singular (`uq_crons_system_activity_type`);
    their body partitions by tenant, not the row (spec §4.6). Keyed on `is_system`, which comes from the
    activity descriptor and never the request, so re-creating a deleted seeded cron still lands in org zero.

    A `None` `author_org` is returned as `None`, never defaulted (`node_identity.node_org`'s rule); it is
    unreachable today, and `crons.org_id` NOT NULL makes it a failed INSERT rather than a misfiled row.
    Leaf function: `app/tenancy/ids.py` is stdlib-only, so a revision may import both."""
    from app.tenancy.ids import ORG_ZERO_ID

    return ORG_ZERO_ID if is_system else author_org


# ── The columns NO HUMAN EVER WRITES ──────────────────────────────────────────────────────────────────
# The four `crons` columns the runtime owns, never written by an operator, API request or seed
# (`schedule_anchor_at`: only `cron_reconcile._re_anchor` and `rephase_anchors`). The `updated_at`
# trigger is scoped to every other column, so a machinery write never reads as a human edit
# on the Automations editor's "Updated by" field. Must agree with the baseline migration's frozen copy;
# `tests/test_cron_updated_at_provenance.py` detects drift when a `crons` column is added.
# `created_by_task_id` (revision 0331) is written only at INSERT; its one UPDATE is the `ON DELETE SET
# NULL` cascade from a deleted task, which must not read as an edit (`project_env_vars.credential_id`'s
# reason, `202610251700_baseline.py::_NARROWED_ENTITY_TRIGGERS`). `automation_id` (revision 0417) is the parent link the
# migration's backfill wrote onto rows nobody edited.
CRON_MACHINERY_COLUMNS: tuple[str, ...] = (
    "last_tick_at", "last_fired_at", "consecutive_skips", "schedule_anchor_at", "last_succeeded_at",
    "created_by_task_id", "automation_id",
)


# ── Why a firing did not run ──────────────────────────────────────────────────────────────────────────
# No CHECK: the wrapper's vocabulary, so a new marker costs no migration. Named here so the Runs tab
# shows one label per skip.
SKIP_ALREADY_RUNNING = "already_running"
SKIP_PREVIOUS_TASK_OPEN = "previous_task_open"
SKIP_DISABLED = "disabled"
# A row saved before its org's minimum cron period was raised, fired again inside it
# (`app/cron_min_period.py`). Not always recorded: a 1s row under a 1h minimum skips 3599 times an hour.
SKIP_BELOW_MIN_PERIOD = "below_min_period"
SKIP_REASONS: tuple[str, ...] = (
    SKIP_ALREADY_RUNNING,
    SKIP_PREVIOUS_TASK_OPEN,
    SKIP_DISABLED,
    SKIP_BELOW_MIN_PERIOD,
)

# Always recorded whatever the retention mode: firings that did not work for a reason nobody chose.
# `disabled` is absent on purpose.
ALWAYS_RECORDED_SKIP_REASONS: tuple[str, ...] = (
    SKIP_ALREADY_RUNNING,
    SKIP_PREVIOUS_TASK_OPEN,
)


def should_record(retention_mode: str, outcome: str, skip_reason: str | None = None) -> bool:
    """The single home for "does this firing earn a `cron_runs` row?" (AC12.2).

    `acted` keeps acted, missed and error firings plus the always-recorded skips, discarding `noop`.
    `none` keeps only `ALWAYS_RECORDED_SKIP_REASONS` plus `error`, so a wedged cron stays visible.
    """
    if outcome == RUN_OUTCOME_ERROR or skip_reason in ALWAYS_RECORDED_SKIP_REASONS:
        return True
    if retention_mode == RETENTION_NONE:
        return False
    if retention_mode == RETENTION_ACTED:
        return outcome in (RUN_OUTCOME_ACTED, RUN_OUTCOME_MISSED)
    # `all`, and any unknown mode: fail loud, since over-recording costs space and under-recording
    # costs debuggability.
    return True
