"""The automation service — the one writer of `automations` rows, the one reader that turns them into the console's
shape (vocabulary: `app/automations.py`).

An automation is a parent row over exactly one When-detail child (`crons` or `task_triggers`). Every child write goes
through the existing child writers (`routes/crons.py::write_cron_*`, `routes/triggers.py::write_trigger_*`), so a
schedule, a source config or a secret meets exactly the refusals it always did; each of those writers calls
`sync_cron` / `sync_trigger` before it commits, so the parent is created or kept in step in the same transaction —
whether the write came from `/api/automations` or the agent verbs (`routes/agent_crons.py`,
`routes/agent_triggers.py`).

FIRING. A tenant automation whose action has a per-target filer fires through `app/automation_fire.py` (schedule,
event and Run now); the parent's Target and params are what it reads. Every save also writes the action onto the
child's own columns, which the child writers validate and the agent verbs read back (a verb's child-only write
re-derives the parent from them, `sync_cron` / `sync_trigger`): a Target the Runner can express rides the `runner`
activity with its `activity_params` filled; any other (nodes, All / All except, more than `MAX_PROJECTS`, a persona
the Runner does not schedule) rides the side-effect-free `noop` carrier, and the parent's action stands
(`carrier_for`). An event child keeps one `project_id` (NOT NULL; preflight and credential lookups read it) — the
rule's first project, or the org's Nodes project for a node target.

The pause stays on the child this release (`crons.enabled`, `task_triggers.paused_at`); `paused` reads and writes it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, literal_column, select, tuple_
from sqlalchemy.orm import Session

from app import automation_actions, target_resolver
from app.automation_actions import AutomationDraft, TargetRule
from app.automations import (
    ACTION_NOOP,
    ACTION_TASK,
    LEGACY_RUNNER_ACTIVITY,
    MODE_ALL,
    MODE_SPECIFIC,
    OUTCOME_ERROR,
    OUTCOME_FILED,
    STATUS_ACTIVE,
    STATUS_FAILING,
    STATUS_PAUSED,
    TARGET_KINDS,
    TARGET_MODES,
    TARGET_NODES,
    TARGET_PROJECTS,
    WHEN_EVENT,
    WHEN_SCHEDULE,
)
from app.cron_schedule import CronScheduleError, describe_schedule_short
from app.crons import (
    RUN_OUTCOME_ACTED,
    RUN_OUTCOME_ERROR,
    RUN_OUTCOME_MISSED,
    RUN_OUTCOME_NOOP,
    RUN_OUTCOME_SKIPPED,
    SKIP_ALREADY_RUNNING,
)
from app.entity_shape import add_with_org_slug, float_or_none, get_or_404, new_uuid7
from app.models.automations import Automation, AutomationRunTarget, Cron, CronRun, TaskTrigger, TriggerDelivery
from app.triggers import (
    DEDUPED,
    DISPATCHED,
    FAILED,
    FILTERED,
    QUEUED,
    RATE_CAPPED,
    SKIPPED,
    TRANSIENT_STATES,
    UNMATCHED,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.authz.principal import Principal
    from app.models.identity import AdminUser

# THE SINGLE HOME of the list's last-run outcome words, one vocabulary for both When kinds: a cron run's
# `acted`/`noop`/`skipped`/`missed`/`error` and a delivery's eleven states each fold onto these seven, so the row
# reads the same whichever child fired. `filed` is `ran` with a Task to show for it; `pending` is a delivery still
# in flight; `error` covers the run itself AND any one target of it. The console's `vocabulary.ts` mirrors the
# words for tone and label only — it never derives one.
LAST_RUN_OUTCOME_FILED = "filed"
LAST_RUN_OUTCOME_RAN = "ran"
LAST_RUN_OUTCOME_NOOP = "noop"
LAST_RUN_OUTCOME_SKIPPED = "skipped"
LAST_RUN_OUTCOME_MISSED = "missed"
LAST_RUN_OUTCOME_ERROR = "error"
LAST_RUN_OUTCOME_PENDING = "pending"
LAST_RUN_OUTCOMES: tuple[str, ...] = (
    LAST_RUN_OUTCOME_FILED,
    LAST_RUN_OUTCOME_RAN,
    LAST_RUN_OUTCOME_NOOP,
    LAST_RUN_OUTCOME_SKIPPED,
    LAST_RUN_OUTCOME_MISSED,
    LAST_RUN_OUTCOME_ERROR,
    LAST_RUN_OUTCOME_PENDING,
)
# A delivery state that already came to nothing (`triggers.py`'s logged non-dispatch outcomes) reads `skipped`.
_SKIPPED_DELIVERY_STATES = (SKIPPED, FILTERED, DEDUPED, RATE_CAPPED, UNMATCHED)
# A delivery not yet settled — the transient states plus a queued one waiting its turn — reads `pending`.
_PENDING_DELIVERY_STATES = TRANSIENT_STATES + (QUEUED,)


# ── The parent's values, derived from a child (the backfill's mapping; revision 0417 mirrors it in SQL) ───────


def _is_task_cron(cron: Cron) -> bool:
    return cron.activity_type == LEGACY_RUNNER_ACTIVITY and not cron.is_system


def values_from_cron(cron: Cron) -> dict[str, Any]:
    """The parent columns a schedule child implies on its own: a tenant Runner is a Task action over its
    `project_ids`; any other activity (noop, a system function) is that activity with its params and no target."""
    params = dict(cron.activity_params or {})
    if _is_task_cron(cron):
        raw_ids = params.get("project_ids")
        return {
            "when_kind": WHEN_SCHEDULE,
            "is_system": False,
            "target_kind": TARGET_PROJECTS,
            "target_mode": MODE_SPECIFIC,
            "target_ids": [pid for pid in raw_ids if isinstance(pid, int) and not isinstance(pid, bool)]
            if isinstance(raw_ids, list) else [],
            "action_type": ACTION_TASK,
            "action_params": {
                "persona": params.get("persona"),
                "body": params.get("instructions"),
                "model": params.get("model") or None,
                "autonomous": params.get("autonomous", True),
                "hidden": params.get("hidden", False),
            },
        }
    return {
        "when_kind": WHEN_SCHEDULE,
        "is_system": bool(cron.is_system),
        "target_kind": None,
        "target_mode": None,
        "target_ids": [],
        "action_type": cron.activity_type,
        "action_params": params,
    }


def values_from_trigger(trigger: TaskTrigger) -> dict[str, Any]:
    """The parent columns an event child implies on its own: a Task action into its one project."""
    return {
        "when_kind": WHEN_EVENT,
        "is_system": False,
        "target_kind": TARGET_PROJECTS,
        "target_mode": MODE_SPECIFIC,
        "target_ids": [trigger.project_id],
        "action_type": ACTION_TASK,
        "action_params": {
            "persona": trigger.persona,
            "body": trigger.prompt_template,
            "title_template": trigger.title_template,
            "model": trigger.model or None,
            "autonomous": bool(trigger.full_auto),
            "hidden": bool(trigger.hidden),
        },
    }


def _merged(existing: Automation, derived: dict[str, Any], *, single_project: bool = False) -> dict[str, Any]:
    """A child-only write (an agent verb) re-derives the parent, keeping what the child
    cannot carry: a Task action's extra params (priority, assignee, …) survive, a persona the child could not hold
    (it stores a fallback) survives, and a Target the child cannot express is left alone — anything but
    projects/specific, and for an event child (`single_project`, one `project_id`) anything but exactly one project."""
    from app.persona_registry import TRIGGER_PERSONAS

    out = dict(derived)
    if single_project and existing.action_type != ACTION_TASK:
        # An event child under an action it cannot express (Send email): the parent's action and Target stand.
        for key in ("action_type", "action_params", "target_kind", "target_mode", "target_ids"):
            out[key] = getattr(existing, key)
        return out
    if existing.action_type == ACTION_TASK and derived["action_type"] == ACTION_TASK:
        params = dict(existing.action_params or {})
        params.update(derived["action_params"])
        if (existing.action_params or {}).get("template_id"):
            # The child holds `TEMPLATE_TRIGGER_BODY`, never a body: the parent files from its template.
            params["body"] = (existing.action_params or {}).get("body") or ""
        if (existing.action_params or {}).get("persona") not in TRIGGER_PERSONAS:
            params["persona"] = (existing.action_params or {}).get("persona")
        out["action_params"] = params
        expressible = existing.target_kind == TARGET_PROJECTS and existing.target_mode == MODE_SPECIFIC and (
            not single_project or len(existing.target_ids or []) == 1)
        if not expressible:
            for key in ("target_kind", "target_mode", "target_ids"):
                out[key] = getattr(existing, key)
    return out


# ── Creating / keeping the parent in step: the hook every child writer calls before it commits ──────────────


@dataclass
class ParentSpec:
    """What a `/api/automations` save decided for the parent, handed to the child writer's hook. `replaces` is the
    other-kind child a When-kind change retires in the same transaction."""

    target_kind: Optional[str]
    target_mode: Optional[str]
    target_ids: list[Any]
    action_type: str
    action_params: dict[str, Any]
    is_system: bool = False
    automation_id: Optional[UUID] = None
    replaces: Any = None
    created_by_task_id: Optional[int] = None
    actor: Any = None


def _automation(session: Session, automation_id: Optional[UUID]) -> Optional[Automation]:
    """One automation by id through the read seam (a filtered select, never a pk `get` the seam cannot filter)."""
    if automation_id is None:
        return None
    return session.scalar(select(Automation).where(Automation.id == automation_id))


def _apply(session: Session, child: Cron | TaskTrigger, values: dict[str, Any], spec: Optional[ParentSpec]) -> Automation:
    """Create the child's parent, or update it, from `values`; link the child; flush."""
    automation = _automation(session, child.automation_id or (spec.automation_id if spec is not None else None))
    created = automation is None
    with session.no_autoflush:
        if automation is None:
            automation = Automation(
                id=new_uuid7(),
                org_id=child.org_id,
                name=child.name,
                description=child.description or "",
                created_by_id=child.created_by_id,
                updated_by_id=child.updated_by_id,
                created_by_task_id=child.created_by_task_id,
            )
        else:
            automation.name = child.name
            automation.description = child.description or ""
            automation.updated_by_id = child.updated_by_id
        for key, value in values.items():
            if getattr(automation, key) != value:
                setattr(automation, key, value)
    if created:
        add_with_org_slug(session, automation, child.name, default="automation")
    session.flush()
    if child.automation_id != automation.id:
        child.automation_id = automation.id
        session.flush()
    if spec is not None and spec.replaces is not None:
        _retire(session, spec.replaces, spec.actor)
    return automation


def _retire(session: Session, child: Cron | TaskTrigger, actor: Any) -> None:
    """Delete the child a When-kind change replaces, recording it as its own writer would (its name is in no request
    field). The new child is already linked, so the parent stays."""
    from app.user_activity import record_write

    kind = "cron" if isinstance(child, Cron) else "trigger"
    record_write(
        session, actor, f"{kind}_delete", f"Deleted {kind} {child.name} (its automation changed When kind)",
        data={f"{kind}_id": str(child.id), "automation_id": str(child.automation_id)},
    )
    session.delete(child)
    session.flush()


def _spec_values(spec: ParentSpec, when_kind: str) -> dict[str, Any]:
    return {
        "when_kind": when_kind,
        "is_system": spec.is_system,
        "target_kind": spec.target_kind,
        "target_mode": spec.target_mode,
        "target_ids": list(spec.target_ids),
        "action_type": spec.action_type,
        "action_params": dict(spec.action_params),
    }


def carrier_activity(action_type: str) -> str:
    """The cron activity a schedule automation's child carries: the Runner for a Task (see `carrier_for` for a
    Target the Runner cannot express), the action's own activity for noop and a system function, and the noop probe
    for a registered action type no cron activity knows (it records a run and files nothing). The fire engine runs a
    Task automation whatever its carrier (`cron_runtime`)."""
    from app import cron_activities

    if action_type == ACTION_TASK:
        return LEGACY_RUNNER_ACTIVITY
    try:
        cron_activities.get(action_type)
    except cron_activities.UnknownActivityError:
        return ACTION_NOOP
    return action_type


def sync_cron(session: Session, cron: Cron, spec: Optional[ParentSpec] = None) -> Automation:
    """The schedule child's parent, created or brought in step. Flushes, never commits."""
    if spec is not None:
        return _apply(session, cron, _spec_values(spec, WHEN_SCHEDULE), spec)
    derived = values_from_cron(cron)
    existing = _automation(session, cron.automation_id)
    if existing is None:
        return _apply(session, cron, derived, None)
    if ((existing.action_type not in (ACTION_TASK, cron.activity_type)
            and carrier_activity(existing.action_type) == cron.activity_type)
            or (existing.action_type == ACTION_TASK and cron.activity_type == ACTION_NOOP)):
        # A child carrying an action it cannot express (see `carrier_activity`, `carrier_for`): the parent's
        # action and Target stand.
        for key in ("action_type", "action_params", "target_kind", "target_mode", "target_ids"):
            derived[key] = getattr(existing, key)
    return _apply(session, cron, _merged(existing, derived), None)


def sync_trigger(session: Session, trigger: TaskTrigger, spec: Optional[ParentSpec] = None) -> Automation:
    """The event child's parent, created or brought in step. Flushes, never commits."""
    if spec is not None:
        return _apply(session, trigger, _spec_values(spec, WHEN_EVENT), spec)
    derived = values_from_trigger(trigger)
    existing = _automation(session, trigger.automation_id)
    return _apply(session, trigger,
                  _merged(existing, derived, single_project=True) if existing is not None else derived, None)


def attachment_keys(session: Session, automation_id: Optional[UUID]) -> list[tuple[Optional[str], str]]:
    """The `(bucket, key)` of an automation's Task-action files — read before the delete that cascades their rows."""
    from app.models.task_extras import TaskAttachment

    if automation_id is None:
        return []
    return sorted(set(session.execute(
        select(TaskAttachment.s3_bucket, TaskAttachment.s3_key).where(TaskAttachment.automation_id == automation_id,
                                                                      TaskAttachment.s3_key.is_not(None))
    ).tuples()), key=lambda ref: (ref[1], ref[0] or ""))


def reap_attachment_keys(session: Session, keys: list[tuple[Optional[str], str]]) -> None:
    """Delete each `(bucket, key)`'s bytes once no row (a fired task's copy) still names the key — after the commit,
    never before. The bucket is the deleted row's own (`TaskAttachment.s3_bucket`)."""
    if not keys:
        return
    from app.attachment_storage import reap_unreferenced_object

    for bucket, key in keys:
        reap_unreferenced_object(session, key, bucket)


# ── Reading ────────────────────────────────────────────────────────────────────────────────────────────────


def visible_clause(principal: "Principal"):
    """The explicit read rule for system rows (AC4.4/AC4.7): a system automation is the operator's alone — omitted
    from a tenant's list and counts and 404 by id — whatever org it happens to be filed in."""
    from sqlalchemy import true

    return true() if principal.is_operator else Automation.is_system.is_(False)


def require(session: Session, principal: "Principal", automation_id: UUID, *, for_update: bool = False) -> Automation:
    """The automation, or one 404 for missing, another org's (the seam) and a system row to a non-operator."""
    return get_or_404(
        session, Automation, automation_id, detail=f"no automation with id {automation_id}",
        where=(visible_clause(principal),), lock=for_update,
    )


def child_of(session: Session, automation: Automation) -> Cron | TaskTrigger:
    model = Cron if automation.when_kind == WHEN_SCHEDULE else TaskTrigger
    child = session.scalar(select(model).where(model.automation_id == automation.id))
    if child is None:  # pragma: no cover - the revision's one-child rule makes this unreachable
        raise HTTPException(status_code=409, detail=f"automation {automation.id} has no {automation.when_kind} detail")
    return child


def is_paused(child: Cron | TaskTrigger) -> bool:
    return (not child.enabled) if isinstance(child, Cron) else child.paused_at is not None


def _ago(at: datetime, now: datetime) -> str:
    seconds = max(0, int((now - at).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def newest_run(session: Session, child: Cron | TaskTrigger) -> Optional[Any]:
    """The newest recorded firing — the Runs list's first row: a `cron_runs` row (`fired_at desc`) or a top-level
    `trigger_deliveries` row (`received_at desc`) — or None when retention kept nothing. Read ONCE per serialized
    automation and handed to both `status_of` and `_last_run_outcome`. For a schedule the two then describe the
    same run; for an event the status keeps reading the newest delivery that REACHED a target (a filtered delivery
    reaches none, and a target that failed is still the operator's problem), so a `skipped` outcome can sit
    beside a `failing` status — each true of the run it names."""
    if isinstance(child, Cron):
        return session.execute(
            select(CronRun.id, CronRun.outcome, CronRun.skip_reason, CronRun.fired_at, CronRun.detail)
            .where(CronRun.cron_id == child.id)
            .order_by(CronRun.fired_at.desc(), CronRun.id.desc()).limit(1)
        ).first()
    return session.execute(
        select(TriggerDelivery.id, TriggerDelivery.state)
        .where(TriggerDelivery.trigger_id == child.id, TriggerDelivery.parent_delivery_id.is_(None))
        .order_by(TriggerDelivery.received_at.desc(), TriggerDelivery.id.desc()).limit(1)
    ).first()


# A raw failure's short human cause, first match wins. Matched against the exception text the engine records
# (`cron_runtime.py` stores `repr(exc)`), case-insensitive. Ordered so a narrower cause wins over a broader one: a
# refused password and a cancelled statement are both psycopg `OperationalError`s, but neither means the database is
# down, so the database bucket names connect failures only. A status code matches only as a whole word.
_FAILURE_CAUSES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (cause, re.compile(pattern, re.IGNORECASE)) for cause, pattern in (
        ("Credential rejected", r"\b401\b|unauthori[sz]ed|bad credentials|invalid token|password authentication failed"),
        ("Timed out", r"statement timeout|timed out|\w*Timeout(Error)?\(|exceeded its \S+ bound|deadline exceeded"),
        ("Database unreachable",
         r"could not connect to server|connection to server|server closed the connection|failed to resolve host"),
        ("Permission denied", r"\b403\b|forbidden|permission denied|PermissionError"),
        ("Rate limited", r"\b429\b|rate limit"),
        ("Service unreachable",
         r"ConnectError|ConnectionError|connection refused|name or service not known|name resolution"
         r"|network is unreachable"),
        ("Not found", r"\b404 not found\b|\bnot found \(404\)|HTTP 404\b"),
    )
)
# `ValueError('…')`, `Traceback`, `psycopg.errors.X`: text a program wrote, not a person.
_EXCEPTION_SHAPED = re.compile(r"^\s*[\w.]*(Error|Exception)\b|^\s*[\w.]+\(|Traceback|\w+\.\w+Error")
_CAUSE_MAX = 60


def failure_cause(detail: Optional[str]) -> str:
    """The short human cause of a failure ("Database unreachable") for the list's one-line failure line; the raw
    `detail` stays in the status sentence (the row's hover) and the run's own page. A detail a person can read
    and that names no known cause is its own cause, its first line cut at `_CAUSE_MAX`; exception text that names
    none reads "Unexpected error"."""
    text = (detail or "").strip()
    if not text:
        return "Failed"
    for cause, pattern in _FAILURE_CAUSES:
        if pattern.search(text):
            return cause
    if _EXCEPTION_SHAPED.search(text):
        return "Unexpected error"
    line = text.splitlines()[0].rstrip(".")
    line = line[:1].upper() + line[1:]
    return line if len(line) <= _CAUSE_MAX else f"{line[:_CAUSE_MAX - 1].rstrip()}…"


def _errored_target_reason(session: Session, run_filter: Any) -> Optional[str]:
    """The reason of one errored target of the run `run_filter` names (a `cron_run_id` or `delivery_id`
    equality), or None when every target of it came to something else."""
    return session.scalar(
        select(AutomationRunTarget.reason).where(run_filter, AutomationRunTarget.outcome == OUTCOME_ERROR).limit(1)
    )


def stuck_run(session: Session, child: Cron | TaskTrigger, run: Optional[Any]) -> Optional[Any]:
    """The firing still holding a schedule's run lock past its hard timeout, or None: the newest run is an
    `already_running` skip, the newest run before those skips is the timeout's `error` row
    (`cron_runtime.ABANDONED_DETAIL_TAIL`), and no firing has started since the abandoned one (`last_fired_at`,
    which only a lock holder stamps, is still that row's `started_at`). Every tick after a wedge is such a skip, so
    without this the newest row would read the cron healthy while nothing runs."""
    from app.cron_runtime import ABANDONED_DETAIL_TAIL

    if not isinstance(child, Cron) or run is None or run.skip_reason != SKIP_ALREADY_RUNNING:
        return None
    holder = session.execute(
        select(CronRun.outcome, CronRun.fired_at, CronRun.started_at, CronRun.detail)
        .where(CronRun.cron_id == child.id, CronRun.skip_reason.is_distinct_from(SKIP_ALREADY_RUNNING))
        .order_by(CronRun.fired_at.desc(), CronRun.id.desc()).limit(1)
    ).first()
    if holder is None or holder.outcome != RUN_OUTCOME_ERROR or ABANDONED_DETAIL_TAIL not in (holder.detail or ""):
        return None
    if holder.started_at is None or (child.last_fired_at is not None and child.last_fired_at > holder.started_at):
        return None  # a later firing took the lock: the abandoned one returned
    return holder


STUCK_CAUSE = "Stuck"


def is_stuck(
    session: Session, child: Cron | TaskTrigger, run: Optional[Any], status: str, cause: Optional[str]
) -> bool:
    """Offers Release (`release_run`): only a stuck schedule has a held run lock worth ending. `status`/`cause` are
    `status_of`'s; a pause hides the cause but not the held lock, so a paused schedule is asked on its own."""
    return cause == STUCK_CAUSE or (status == STATUS_PAUSED and stuck_run(session, child, run) is not None)


def status_of(
    session: Session, automation: Automation, child: Cron | TaskTrigger, now: datetime, run: Optional[Any],
    errored: Optional[str] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    """`paused`, else `failing` (an event the system stopped, a schedule whose newest recorded run errored, or a
    newest run with an errored target), else `active` — with the sentence saying why (§6.3) and, when failing,
    that failure's short human cause (`failure_cause`). `run` is
    `newest_run(session, child)` and `errored` its one errored target's reason (`errored_target_of`, read once
    by `out` for the outcome word too); an event child's branch reads the target ledger instead and ignores both."""
    if is_paused(child):
        return STATUS_PAUSED, None, None
    if isinstance(child, TaskTrigger):
        if child.failed_at is not None:
            return (STATUS_FAILING, f"Stopped {_ago(child.failed_at, now)}: {child.failed_reason or 'failed'}",
                    failure_cause(child.failed_reason))
        latest_run = session.scalar(
            select(AutomationRunTarget.delivery_id).where(AutomationRunTarget.automation_id == automation.id,
                                                          AutomationRunTarget.delivery_id.is_not(None))
            .order_by(AutomationRunTarget.delivery_id.desc()).limit(1)
        )
        errored = latest_run is not None and _errored_target_reason(
            session, AutomationRunTarget.delivery_id == latest_run
        )
        if errored:
            return STATUS_FAILING, f"A target failed on the last run: {errored}", failure_cause(errored)
        return STATUS_ACTIVE, None, None
    if run is None:
        return STATUS_ACTIVE, None, None
    stuck = stuck_run(session, child, run)
    if stuck is not None:
        return (STATUS_FAILING,
                f"Stuck: the run {_ago(stuck.fired_at, now)} {stuck.detail}; every run since was skipped",
                STUCK_CAUSE)
    if run.outcome == RUN_OUTCOME_ERROR:
        return (STATUS_FAILING, f"Last run failed {_ago(run.fired_at, now)}: {run.detail or 'error'}",
                failure_cause(run.detail))
    if errored:
        return (STATUS_FAILING, f"A target failed on the last run {_ago(run.fired_at, now)}: {errored}",
                failure_cause(errored))
    return STATUS_ACTIVE, None, None


def errored_target_of(session: Session, child: Cron | TaskTrigger, run: Optional[Any]) -> Optional[str]:
    """A schedule's newest run's one errored target reason, read once for the status sentence and the outcome word.
    None for an event child: its status reads the ledger by its own rule (`status_of`)."""
    if run is None or not isinstance(child, Cron):
        return None
    return _errored_target_reason(session, AutomationRunTarget.cron_run_id == run.id)


def _any_target(session: Session, run_filter: Any, outcome: str) -> bool:
    return session.scalar(
        select(AutomationRunTarget.id).where(run_filter, AutomationRunTarget.outcome == outcome).limit(1)
    ) is not None


def _last_run_outcome(
    session: Session, child: Cron | TaskTrigger, run: Optional[Any], errored: Optional[str]
) -> Optional[str]:
    """One of `LAST_RUN_OUTCOMES` for `run` (`newest_run`'s row), or None when no run row survives — the child's
    own `last_fired_at` / `last_delivery_at` still dates the run then, and its outcome is honestly unknown rather
    than guessed. A run the engine counts as a success still reads `error` when any one target of it errored:
    the row is the operator's glance, and a half-failed run is not a green one."""
    if run is None:
        return None
    if isinstance(child, Cron):
        if _fired_after_gap(child, run):
            return None
        if run.outcome == RUN_OUTCOME_ERROR or errored:
            return LAST_RUN_OUTCOME_ERROR
        if run.outcome == RUN_OUTCOME_ACTED:
            filed = _any_target(session, AutomationRunTarget.cron_run_id == run.id, OUTCOME_FILED)
            return LAST_RUN_OUTCOME_FILED if filed else LAST_RUN_OUTCOME_RAN
        if run.outcome in (RUN_OUTCOME_NOOP, RUN_OUTCOME_SKIPPED, RUN_OUTCOME_MISSED):
            return run.outcome
        return LAST_RUN_OUTCOME_RAN
    if run.state == DISPATCHED:
        return _dispatched_outcome(session, run.id)
    if run.state == FAILED:
        return LAST_RUN_OUTCOME_ERROR
    if run.state in _SKIPPED_DELIVERY_STATES:
        return LAST_RUN_OUTCOME_SKIPPED
    if run.state in _PENDING_DELIVERY_STATES:
        return LAST_RUN_OUTCOME_PENDING
    return LAST_RUN_OUTCOME_RAN


def _fired_after_gap(child: Cron, run: Any) -> bool:
    """Is the newest row a backfilled `missed` occurrence that a later firing left behind? The backfill only ever
    runs inside a firing (`cron_runtime._backfill_missed_ledger`), so a newer `last_fired_at` is that firing, which
    the row's retention mode kept no row of (`crons.should_record`: `acted` drops a `noop`). Its outcome is then
    unknown, never the gap's `missed`: a restart would otherwise read as a failure until the cron next acts."""
    return run.outcome == RUN_OUTCOME_MISSED and child.last_fired_at is not None and child.last_fired_at > run.fired_at


# One backfill writes its rows in one pass, so their uuid7 ids carry one write time; a `missed` row written this
# much earlier is an older restart's, which an `acted` row never recorded between (a noop leaves no row).
_BACKFILL_BATCH_MS = 60_000


def _uuid7_ms(value: UUID) -> int:
    """The Unix ms a uuid7 was minted at (`entity_shape.new_uuid7`, the DB's `uuidv7()`)."""
    return int.from_bytes(value.bytes[:6], "big")


def _missed_gap(session: Session, child: Cron | TaskTrigger, run: Optional[Any]) -> Optional[dict[str, Any]]:
    """The newest restart's backfill (`cron_runtime._backfill_missed_ledger`) as ONE fact, so a restart reads as one
    event on the list: `count` rows, `more` when the backfill capped it (only the capped row carries a detail,
    saying how many it left out). None when the newest row is not `missed`."""
    from app.cron_runtime import MISSED_ROW_CAP

    if run is None or not isinstance(child, Cron) or run.outcome != RUN_OUTCOME_MISSED:
        return None
    # Cut in `newest_run`'s own order, (fired_at, id): a `missed` row can share its slot's instant with the firing
    # that already ran it, and a fired_at-only cut then dropped the very row `run` is.
    newest_other = session.execute(
        select(CronRun.fired_at, CronRun.id).where(CronRun.cron_id == child.id, CronRun.outcome != RUN_OUTCOME_MISSED)
        .order_by(CronRun.fired_at.desc(), CronRun.id.desc()).limit(1)
    ).first()
    # A backfill writes at most `MISSED_ROW_CAP` rows, so the newest batch is within the newest that many.
    query = (
        select(CronRun.id, CronRun.detail).where(CronRun.cron_id == child.id, CronRun.outcome == RUN_OUTCOME_MISSED)
        .order_by(CronRun.fired_at.desc(), CronRun.id.desc()).limit(MISSED_ROW_CAP)
    )
    if newest_other is not None:
        query = query.where(tuple_(CronRun.fired_at, CronRun.id) > tuple(newest_other))
    rows = session.execute(query).all()
    written = max(_uuid7_ms(row.id) for row in rows)
    batch = [row for row in rows if written - _uuid7_ms(row.id) < _BACKFILL_BATCH_MS]
    return {"count": len(batch), "more": any(row.detail for row in batch)}


def _dispatched_outcome(session: Session, delivery_id: int) -> str:
    """A DISPATCHED delivery's word. A plain delivery dispatched its own task, so its ledger decides: an errored
    target is `error`, a filed one `filed`, neither (a delivery older than the ledger) `ran`. A preflight FAN-OUT's
    parent is marked dispatched the moment its items are logged (`trigger_intake.py`), before any of them has
    filed, and its targets ride the CHILD deliveries — so the children's ledger and states fold in: one still in
    flight is `pending`, none filed and none in flight is `skipped`, a failed child `error`."""
    children = session.scalars(
        select(TriggerDelivery.state).where(TriggerDelivery.parent_delivery_id == delivery_id)
    ).all()
    mine = AutomationRunTarget.delivery_id == delivery_id
    of_family = AutomationRunTarget.delivery_id.in_(
        select(TriggerDelivery.id).where(TriggerDelivery.parent_delivery_id == delivery_id)
    )
    if _any_target(session, mine, OUTCOME_ERROR) or (children and _any_target(session, of_family, OUTCOME_ERROR)):
        return LAST_RUN_OUTCOME_ERROR
    if FAILED in children:
        return LAST_RUN_OUTCOME_ERROR
    if _any_target(session, mine, OUTCOME_FILED) or (children and _any_target(session, of_family, OUTCOME_FILED)):
        return LAST_RUN_OUTCOME_FILED
    if not children:
        return LAST_RUN_OUTCOME_RAN
    if any(state in _PENDING_DELIVERY_STATES for state in children):
        return LAST_RUN_OUTCOME_PENDING
    if DISPATCHED in children:
        # A child dispatched before the ledger existed: it filed something, even if nothing records what.
        return LAST_RUN_OUTCOME_FILED
    return LAST_RUN_OUTCOME_SKIPPED


def _when_out(child: Cron | TaskTrigger, now: datetime) -> dict[str, Any]:
    if isinstance(child, Cron):
        from app.routes.crons import _schedule_human

        return {
            "kind": WHEN_SCHEDULE,
            "schedule_kind": child.schedule_kind,
            "cron_expression": child.cron_expression,
            "interval_seconds": child.interval_seconds,
            "retention_mode": child.retention_mode,
            "min_gap_seconds": float_or_none(child.min_gap_seconds),
            "timeout_seconds": float_or_none(child.timeout_seconds),
            "schedule_human": _zoned(_schedule_human(child), child, now),
            "schedule_short": _short_zoned(_schedule_short(child), child, now),
        }
    from app import trigger_health
    from app.routes.triggers import _url_for

    state, reason = trigger_health.trigger_health(child)
    return {
        "kind": WHEN_EVENT,
        "source_kind": child.source_kind,
        "source_config": child.source_config or {},
        "preflight_kind": child.preflight_kind,
        "preflight_script_path": child.preflight_script_path,
        "preflight_prompt": child.preflight_prompt,
        "preflight_credential_id": str(child.preflight_credential_id) if child.preflight_credential_id else None,
        "preflight_timeout_seconds": child.preflight_timeout_seconds,
        "concurrency_cap": child.concurrency_cap,
        "queue_depth": child.queue_depth,
        "rate_limit_per_hour": child.rate_limit_per_hour,
        "budget_usd": float(child.budget_usd) if child.budget_usd is not None else None,
        "auto_pause_failures": child.auto_pause_failures,
        "auto_pause_idle_days": child.auto_pause_idle_days,
        "ingress_url": _url_for(child),
        "secret_state": (
            "none" if child.secret_sealed is None else "revealed" if child.secret_revealed_at is not None else "owed"
        ),
        "health": {"state": state, "reason": reason},
        "state_document": child.state_document or {},
    }


def _schedule_short(cron: Cron) -> Optional[str]:
    """`describe_schedule_short`, with a row it cannot read at all (raw-SQL writers; `_schedule_human`'s same
    defence) reading None like an irregular one: the client shows the sentence either way."""
    try:
        return describe_schedule_short(cron.schedule_kind, cron.cron_expression, cron.interval_seconds)
    except CronScheduleError:
        return None


def _zone_abbr(cron: Cron, now: datetime) -> Optional[str]:
    """The cron zone's abbreviation now ("EDT") for a clock-time schedule, else None: the cron zone is one
    control-plane knob. A cadence ("Every 5 minutes") and an interval have no clock time."""
    from app.cron_runtime import cron_timezone

    fields = (cron.cron_expression or "").split()
    hour = fields[-4] if cron.schedule_kind == "cron" and len(fields) >= 5 else "*"
    if hour.startswith("*"):
        return None
    return now.astimezone(cron_timezone()).strftime("%Z")


def _zoned(sentence: str, cron: Cron, now: datetime) -> str:
    """A clock-time schedule's sentence with the zone it runs in named ("At 09:00 AM (EDT)"): a sentence without
    it reads as the viewer's clock."""
    zone = _zone_abbr(cron, now)
    return f"{sentence} ({zone})" if zone else sentence


def _short_zoned(phrase: Optional[str], cron: Cron, now: datetime) -> Optional[str]:
    """`_zoned` for the list row's compact phrase: "Daily 04:05 EDT"; "Every 5m" stays as it is."""
    zone = _zone_abbr(cron, now) if phrase else None
    return f"{phrase} {zone}" if zone else phrase


def _latest(*instants: Optional[datetime]) -> Optional[datetime]:
    present = [at for at in instants if at is not None]
    return max(present) if present else None


def _runs_times(session: Session, child: Cron | TaskTrigger, now: datetime) -> tuple[Optional[datetime], Optional[datetime]]:
    """Last run is the newest run of ANY outcome (the Runs list's first row), so a run that skipped every target
    still counts; the child's own stamp covers runs retention has pruned."""
    if isinstance(child, Cron):
        from app.routes.crons import _next_fire

        newest = session.scalar(select(func.max(CronRun.fired_at)).where(CronRun.cron_id == child.id))
        return _latest(child.last_fired_at, newest), (_next_fire(child, now) if child.enabled else None)
    newest = session.scalar(
        select(func.max(TriggerDelivery.received_at)).where(TriggerDelivery.trigger_id == child.id,
                                                            TriggerDelivery.parent_delivery_id.is_(None))
    )
    return _latest(child.last_delivery_at, newest), None


def _last_acted_at(session: Session, child: Cron | TaskTrigger) -> Optional[datetime]:
    """When a schedule last did work: its newest recorded `acted` run, so a row whose newest firing had nothing to
    do still says the work is happening ("last did work 3h ago"). None for an event (a delivery has no `noop`) and
    for a schedule whose retention kept no `acted` row (`none` keeps no success)."""
    if not isinstance(child, Cron):
        return None
    # The outcome as SQL text, not a bind: a prepared statement's generic plan cannot prove `outcome = $1` matches
    # the partial index's `outcome = 'acted'` (`ix_cron_runs_cron_id_acted`).
    return session.scalar(
        select(func.max(CronRun.fired_at))
        .where(CronRun.cron_id == child.id, CronRun.outcome == literal_column(f"'{RUN_OUTCOME_ACTED}'"))
    )


def out(session: Session, automation: Automation, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """The §6.3 object shape."""
    from app.actors import actor_names
    from app.entity_shape import iso_or_none

    now = now or datetime.now(timezone.utc)
    child = child_of(session, automation)
    run = newest_run(session, child)
    errored = errored_target_of(session, child, run)
    status, reason, cause = status_of(session, automation, child, now, run, errored)
    last_run, next_run = _runs_times(session, child, now)
    names = actor_names(session, [automation.created_by_id, automation.updated_by_id])
    action_params = (
        automation_actions.task_params(automation.action_params)
        if automation.action_type == ACTION_TASK else dict(automation.action_params or {})
    )
    return {
        "id": str(automation.id),
        "name": automation.name,
        "slug": automation.slug,
        "description": automation.description,
        "org_id": str(automation.org_id),
        "owner": {"id": str(automation.created_by_id), "name": names.get(automation.created_by_id)},
        "is_system": automation.is_system,
        "when": _when_out(child, now),
        "target": (
            None if automation.target_kind is None
            else {"kind": automation.target_kind, "mode": automation.target_mode, "ids": list(automation.target_ids)}
        ),
        "action": {"type": automation.action_type, "params": action_params},
        "paused": is_paused(child),
        "status": status,
        "status_reason": reason,
        "status_cause": cause,
        "stuck": is_stuck(session, child, run, status, cause),
        "last_run_at": iso_or_none(last_run),
        "last_run_outcome": _last_run_outcome(session, child, run, errored),
        "last_run_missed": _missed_gap(session, child, run),
        "last_acted_at": iso_or_none(_last_acted_at(session, child)),
        "next_run_at": iso_or_none(next_run),
        "legacy": {"kind": "cron" if isinstance(child, Cron) else "trigger", "id": str(child.id)},
        "created_by_task_id": automation.created_by_task_id,
        "created_at": iso_or_none(automation.created_at),
        "updated_at": iso_or_none(automation.updated_at),
        "updated_by": {"id": str(automation.updated_by_id), "name": names.get(automation.updated_by_id)},
    }


# ── Saving through `/api/automations` ──────────────────────────────────────────────────────────────────────


def target_rule(raw: Any) -> Optional[TargetRule]:
    """The wire `TargetRule` (or null), shape-checked; the universe check is `validated_target`'s."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="target: must be an object or null")
    kind, mode, ids = raw.get("kind"), raw.get("mode"), raw.get("ids", [])
    if kind not in TARGET_KINDS:
        raise HTTPException(status_code=422, detail=f"target.kind: must be one of {', '.join(TARGET_KINDS)}")
    if mode not in TARGET_MODES:
        raise HTTPException(status_code=422, detail=f"target.mode: must be one of {', '.join(TARGET_MODES)}")
    if not isinstance(ids, list):
        raise HTTPException(status_code=422, detail="target.ids: must be a list")
    try:
        normalized = target_resolver.normalize_ids(kind, ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"target.ids: {exc}") from exc
    if mode == MODE_ALL and normalized:
        raise HTTPException(status_code=422, detail="target.ids: All takes no ids — use All except to leave some out")
    return TargetRule(kind=kind, mode=mode, ids=tuple(normalized))


def _noun(kind: str) -> str:
    return "project" if kind == TARGET_PROJECTS else "node"


def validated_target(
    session: Session, descriptor: automation_actions.ActionDescriptor, org_id: UUID, rule: Optional[TargetRule],
    *, stored: Optional[Automation] = None,
) -> Optional[TargetRule]:
    """The Target a save may store: required iff the action takes one, of a kind the action accepts, never the
    empty "None selected" state, and every id inside the org's universe (AC2.3) — except an id already stored on
    this automation, which survives as dangling until the user removes it (AC3.7)."""
    if not descriptor.takes_target:
        if rule is not None:
            raise HTTPException(status_code=422, detail=f"target: a {descriptor.label} action takes no target")
        return None
    if rule is None:
        raise HTTPException(status_code=422, detail=f"target: a {descriptor.label} action needs a target")
    if rule.kind not in descriptor.target_kinds:
        raise HTTPException(
            status_code=422, detail=f"target.kind: a {descriptor.label} action targets {', '.join(descriptor.target_kinds)}"
        )
    if rule.mode == MODE_SPECIFIC and not rule.ids:
        raise HTTPException(status_code=422, detail=f"target: Pick at least one {_noun(rule.kind)}")
    known = {target_resolver.target_key(rule.kind, item.id) for item in target_resolver.universe(session, org_id, rule.kind)}
    kept: set[str] = set()
    if stored is not None and stored.target_kind == rule.kind:
        kept = {target_resolver.target_key(rule.kind, value) for value in stored.target_ids or []}
    for value in rule.ids:
        key = target_resolver.target_key(rule.kind, value)
        if key not in known and key not in kept:
            raise HTTPException(
                status_code=422,
                detail=f"target.ids: {_noun(rule.kind)} {value} is not one this organization's automations may target",
            )
    return rule


def runner_expressible(rule: Optional[TargetRule], params: dict[str, Any]) -> bool:
    """Can the legacy Runner's `activity_params` hold this Task action (one to `MAX_PROJECTS` specific projects and
    a persona the Runner schedules)? Decides the schedule child's carrier (`carrier_for`)."""
    from app.cron_activity_runner import MAX_PROJECTS
    from app.persona_registry import SCHEDULABLE_PERSONAS

    return (
        rule is not None and rule.kind == TARGET_PROJECTS and rule.mode == MODE_SPECIFIC
        and 1 <= len(rule.ids) <= MAX_PROJECTS and params.get("persona") in SCHEDULABLE_PERSONAS
        and not params.get("template_id")
    )


def carrier_for(descriptor: automation_actions.ActionDescriptor, rule: Optional[TargetRule],
                params: dict[str, Any]) -> str:
    """The schedule child's activity for this action: `carrier_activity`, except that a Task action the Runner
    cannot express rides `noop`: a Runner bag cannot hold it (`check_project_keys`, `MAX_PROJECTS`), and `sync_cron`
    reads a `noop` child under a Task parent as "the parent's action and Target stand"."""
    if descriptor.type == ACTION_TASK and not runner_expressible(rule, params):
        return ACTION_NOOP
    return carrier_activity(descriptor.type)


def legacy_cron_params(rule: TargetRule, params: dict[str, Any]) -> dict[str, Any]:
    """The Runner's `activity_params` a schedule Task action writes onto its cron — validated by `write_cron_*`,
    merged by the agent cron verb and read back by `values_from_cron`; only for a `runner_expressible` action."""
    from app.model_catalog import INHERIT

    return {
        "instructions": params["body"],
        "project_ids": [int(pid) for pid in rule.ids],
        "persona": params["persona"],
        "model": params.get("model") or INHERIT,
        "hidden": bool(params.get("hidden", False)),
        "autonomous": bool(params.get("autonomous", True)),
    }


#: The `prompt_template` an event child of an action with no Target carries: NOT NULL, never rendered.
UNTARGETED_TRIGGER_BODY = "(this automation sends an email; it files no Task)"
#: The `prompt_template` an event child of a Task action filed from a template carries: NOT NULL, never rendered.
TEMPLATE_TRIGGER_BODY = "(this automation files its Task from a template)"


def legacy_trigger_fields(session: Session, org_id: UUID, rule: Optional[TargetRule], params: dict[str, Any]) -> dict[str, Any]:
    """The trigger columns an event Task action writes — NOT NULL and validated by `write_trigger_*`, read by
    `values_from_trigger`, the agent trigger verbs, preflight and credential lookups. The child holds one project:
    a Specific rule's first, else the first the rule resolves to now, else the org's Nodes project (a node target,
    or a rule resolving to nothing). A persona a trigger cannot hold is stored as `coder`;
    the parent's stands (`_merged`)."""
    from app import node_targets
    from app.persona_registry import TRIGGER_PERSONAS

    if rule is None:
        # An action with no Target (Send email): the child holds the org's Nodes project and a fixed body it never
        # files; the parent's action stands (`_merged`).
        return {
            "project_id": node_targets.ensure_nodes_project(session, org_id).id, "persona": "coder",
            "prompt_template": UNTARGETED_TRIGGER_BODY, "title_template": None, "model": None, "full_auto": True,
            "hidden": False,
        }
    return {
        "project_id": _carrier_project(session, org_id, rule),
        "persona": params["persona"] if params["persona"] in TRIGGER_PERSONAS else "coder",
        "prompt_template": TEMPLATE_TRIGGER_BODY if params.get("template_id") else params["body"],
        "title_template": params.get("title_template"),
        "model": params.get("model"),
        "full_auto": bool(params.get("autonomous", True)),
        "hidden": bool(params.get("hidden", False)),
    }


def _carrier_project(session: Session, org_id: UUID, rule: TargetRule) -> int:
    from app import node_targets

    if rule.kind == TARGET_PROJECTS:
        if rule.mode == MODE_SPECIFIC and rule.ids:
            return int(rule.ids[0])
        resolved = target_resolver.apply_rule(
            target_resolver.universe(session, org_id, TARGET_PROJECTS), rule.kind, rule.mode, rule.ids)
        if resolved.fire:
            return int(resolved.fire[0].id)
    return node_targets.ensure_nodes_project(session, org_id).id


SCHEDULE_FIELDS = ("schedule_kind", "cron_expression", "interval_seconds", "retention_mode", "min_gap_seconds",
                   "timeout_seconds")
EVENT_FIELDS = (
    "source_kind", "source_config", "preflight_kind", "preflight_script_path", "preflight_prompt",
    "preflight_credential_id", "preflight_timeout_seconds", "concurrency_cap", "queue_depth", "rate_limit_per_hour",
    "budget_usd", "auto_pause_failures", "auto_pause_idle_days",
)


@dataclass
class Saved:
    automation: Automation
    secret: Optional[str] = None


def _validated_action(
    session: Session, *, org_id: UUID, when_kind: str, raw_target: Any, action: dict[str, Any],
    actor: "AdminUser", principal: "Principal", stored: Optional[Automation] = None,
) -> tuple[automation_actions.ActionDescriptor, Optional[TargetRule], dict[str, Any]]:
    """The action's descriptor (refused if this caller may not use it), the validated Target, and the normalised
    params — in that order, so a bad type is named before its params are judged."""
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        raise HTTPException(status_code=422, detail="action.type: required")
    descriptor = automation_actions.get(action["type"])
    if not automation_actions.may_use(descriptor, principal):
        raise HTTPException(
            status_code=422,
            detail=(
                f"action.type: {descriptor.label} is control-plane machinery that acts across every organization, "
                "so only an operator may schedule or edit it"
            ),
        )
    if descriptor.system_activity is not None and when_kind != WHEN_SCHEDULE:
        raise HTTPException(status_code=422, detail="when.kind: a system function runs on a schedule")
    if not descriptor.event_capable and when_kind == WHEN_EVENT:
        raise HTTPException(status_code=422, detail=f"when.kind: {descriptor.label} runs on a schedule, not on an event")
    rule = validated_target(session, descriptor, org_id, target_rule(raw_target), stored=stored)
    params = action.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="action.params: must be an object")
    draft = AutomationDraft(
        org_id=org_id, when_kind=when_kind, target=rule, actor=actor, principal=principal,
        automation_id=stored.id if stored is not None else None,
    )
    params = descriptor.validate(session, draft, params)
    return descriptor, rule, params


def _cron_activity(descriptor: automation_actions.ActionDescriptor, rule, params: dict[str, Any]) -> tuple[str, Any]:
    from app import cron_activities

    activity_type = carrier_for(descriptor, rule, params)
    return activity_type, cron_activities.get(activity_type)


def _carried_params(descriptor: automation_actions.ActionDescriptor, rule, params: dict[str, Any]) -> dict[str, Any]:
    """The `activity_params` the schedule child stores for this action (see `carrier_for`)."""
    carrier = carrier_for(descriptor, rule, params)
    if descriptor.type == ACTION_TASK:
        return legacy_cron_params(rule, params) if carrier != ACTION_NOOP else {}
    return params if carrier == descriptor.type else {}


def _spec(descriptor, rule: Optional[TargetRule], params: dict[str, Any], **extra: Any) -> ParentSpec:
    return ParentSpec(
        target_kind=rule.kind if rule is not None else None,
        target_mode=rule.mode if rule is not None else None,
        target_ids=list(rule.ids) if rule is not None else [],
        action_type=descriptor.type,
        action_params=params,
        is_system=descriptor.system_activity is not None,
        **extra,
    )


def _cron_create_body(name: str, description: str, org_id: UUID, when: dict[str, Any], descriptor, rule, params,
                      *, enabled: bool):
    from app.routes.crons import CronCreate

    activity_type, _activity = _cron_activity(descriptor, rule, params)
    activity_params = _carried_params(descriptor, rule, params)
    fields = {key: when[key] for key in SCHEDULE_FIELDS if key in when}
    try:
        return CronCreate(
            org_id=org_id, name=name, description=description, activity_type=activity_type,
            activity_params=activity_params, enabled=enabled, **fields,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"when: {exc}") from exc


def _trigger_create_body(session: Session, org_id: UUID, name: str, description: str, when: dict[str, Any], rule,
                         params):
    from app.routes.triggers import TriggerCreate

    fields = {key: when[key] for key in EVENT_FIELDS if key in when}
    try:
        return TriggerCreate(name=name, description=description, **legacy_trigger_fields(session, org_id, rule, params),
                             **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"when: {exc}") from exc


def create(
    session: Session, *, org_id: UUID, body: dict[str, Any], actor: "AdminUser", principal: "Principal",
    background: Any,
) -> Saved:
    """One new automation and its When child, in one transaction (the child writer commits)."""
    when = body.get("when") or {}
    when_kind = when.get("kind")
    if when_kind not in (WHEN_SCHEDULE, WHEN_EVENT):
        raise HTTPException(status_code=422, detail=f"when.kind: must be {WHEN_SCHEDULE!r} or {WHEN_EVENT!r}")
    descriptor, rule, params = _validated_action(
        session, org_id=org_id, when_kind=when_kind, raw_target=body.get("target"), action=body.get("action") or {},
        actor=actor, principal=principal,
    )
    if not descriptor.creatable:
        raise HTTPException(
            status_code=422,
            detail=f"action.type: {descriptor.label} cannot be created again; it is offered only where it already runs",
        )
    name, description = body["name"], body.get("description", "")
    paused = bool(body.get("paused", False))
    spec = _spec(descriptor, rule, params)
    if when_kind == WHEN_SCHEDULE:
        from app.crons import org_for
        from app.routes.crons import write_cron_create

        activity_type, activity = _cron_activity(descriptor, rule, params)
        cron_org = org_for(is_system=activity.is_system, author_org=org_id)
        cron_body = _cron_create_body(name, description, org_id, when, descriptor, rule, params, enabled=not paused)
        detail = write_cron_create(session, cron_body, actor, background, descriptor=activity, org_id=cron_org,
                                   automation=spec)
        automation_id = session.scalar(select(Cron.automation_id).where(Cron.id == detail.id))
        return Saved(automation=_automation(session, automation_id))
    from app.routes.triggers import write_trigger_create

    trigger, secret = write_trigger_create(
        session, _trigger_create_body(session, org_id, name, description, when, rule, params), actor, automation=spec,
        paused=paused)
    return Saved(automation=_automation(session, trigger.automation_id), secret=secret)


def update(
    session: Session, automation: Automation, *, body: dict[str, Any], actor: "AdminUser", principal: "Principal",
    background: Any,
) -> Saved:
    """Patch one automation. Sent keys only; the merged automation is validated whole. Changing the When kind
    swaps the child in one transaction (a new event child answers its secret once)."""
    child = child_of(session, automation)
    for key in ("name", "description"):
        if key in body and body[key] is None:
            raise HTTPException(status_code=422, detail=f"{key}: cannot be null")
    when = body.get("when")
    if when is not None and not isinstance(when, dict):
        raise HTTPException(status_code=422, detail="when: must be an object")
    new_kind = (when or {}).get("kind", automation.when_kind)
    action = body.get("action")
    if action is None:
        action = {"type": automation.action_type}
    elif not isinstance(action, dict):
        raise HTTPException(status_code=422, detail="action: must be an object")
    if action.get("type") == automation.action_type:
        # Sent keys only: a same-type action's params merge over the stored ones.
        action = {**action, "params": {**(automation.action_params or {}), **(action.get("params") or {})}}
        if new_kind == WHEN_SCHEDULE and automation.when_kind == WHEN_EVENT and "title_template" not in (
            (body.get("action") or {}).get("params") or {}
        ):
            # An event's title template has no meaning on a schedule; moving to one drops it.
            action["params"].pop("title_template", None)
    else:
        action = {**action, "params": action.get("params") or {}}
    if action.get("type") != automation.action_type:
        new = automation_actions.lookup(action.get("type") or "")
        if automation.is_system or (new is not None and new.system_activity is not None):
            raise HTTPException(
                status_code=422,
                detail="action.type: a system function cannot be swapped for another action; create a new automation",
            )
    raw_target = body["target"] if "target" in body else (
        None if automation.target_kind is None
        else {"kind": automation.target_kind, "mode": automation.target_mode, "ids": list(automation.target_ids)}
    )
    descriptor, rule, params = _validated_action(
        session, org_id=automation.org_id, when_kind=new_kind, raw_target=raw_target, action=action, actor=actor,
        principal=principal, stored=automation,
    )
    name = body.get("name", automation.name)
    description = body.get("description", automation.description)
    spec = _spec(descriptor, rule, params, automation_id=automation.id, actor=actor)
    secret: Optional[str] = None
    if new_kind != automation.when_kind:
        spec.replaces = child
        if new_kind == WHEN_SCHEDULE:
            from app.routes.crons import write_cron_create

            _activity_type, activity = _cron_activity(descriptor, rule, params)
            if not isinstance(when, dict) or not when.get("schedule_kind"):
                raise HTTPException(status_code=422, detail="when.schedule_kind: required for a schedule")
            write_cron_create(
                session, _cron_create_body(name, description, automation.org_id, when, descriptor, rule, params,
                                           enabled=not body.get("paused", is_paused(child))),
                actor, background, descriptor=activity, org_id=automation.org_id, automation=spec,
            )
        else:
            from app.routes.triggers import write_trigger_create

            if not isinstance(when, dict) or not when.get("source_kind"):
                raise HTTPException(status_code=422, detail="when.source_kind: required for an event")
            from app.routes.crons import _reconcile_later

            _trigger, secret = write_trigger_create(
                session, _trigger_create_body(session, automation.org_id, name, description, when, rule, params), actor,
                automation=spec,
                paused=bool(body.get("paused", is_paused(child))),
            )
            # The retired schedule's engine entry goes on the next reconcile, as a cron delete's does.
            _reconcile_later(background)
        session.expire_all()
        refreshed = _automation(session, automation.id)
        _stamp(session, refreshed, actor)
        session.commit()
        return Saved(automation=refreshed, secret=secret)

    if isinstance(child, Cron):
        from app.routes.crons import CronUpdate, write_cron_update

        patch: dict[str, Any] = {key: when[key] for key in SCHEDULE_FIELDS if when and key in when}
        activity_type, _activity = _cron_activity(descriptor, rule, params)
        if activity_type != child.activity_type:
            patch["activity_type"] = activity_type
        carried = _carried_params(descriptor, rule, params)
        if carried != (child.activity_params or {}):
            patch["activity_params"] = carried
        for key in ("name", "description"):
            if key in body:
                patch[key] = body[key]
        if "paused" in body:
            patch["enabled"] = not bool(body["paused"])
        locked = session.scalar(select(Cron).where(Cron.id == child.id).with_for_update())
        try:
            cron_patch = CronUpdate(**patch)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"when: {exc}") from exc
        write_cron_update(session, locked, cron_patch, actor, background, automation=spec)
        sync_cron(session, locked, spec)
    else:
        from app.routes.triggers import TriggerUpdate, write_pause, write_resume, write_trigger_update

        patch = {key: when[key] for key in EVENT_FIELDS if when and key in when}
        patch.update(legacy_trigger_fields(session, automation.org_id, rule, params))
        for key in ("name", "description"):
            if key in body:
                patch[key] = body[key]
        locked = session.scalar(select(TaskTrigger).where(TaskTrigger.id == child.id).with_for_update())
        try:
            trigger_patch = TriggerUpdate(**patch)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"when: {exc}") from exc
        write_trigger_update(session, locked, trigger_patch, actor, automation=spec)
        sync_trigger(session, locked, spec)
        if "paused" in body and bool(body["paused"]) != is_paused(locked):
            (write_pause(session, locked, actor, "") if body["paused"] else write_resume(session, locked, actor))
    _stamp(session, automation, actor)
    session.commit()
    session.refresh(automation)
    return Saved(automation=automation, secret=secret)


def _stamp(session: Session, automation: Automation, actor: "AdminUser") -> None:
    """The person who saved is the parent's editor, even when only parent-held fields moved (the child writer then
    returns early and stamps nobody)."""
    from app.actors import stamp_actor

    stamp_actor(automation, actor)


def delete(session: Session, automation: Automation, *, actor: "AdminUser", background: Any) -> None:
    """Delete the automation by deleting its child through the child's own writer (audit, DBOS reconcile); the
    revision's trigger deletes the parent it leaves childless."""
    child = child_of(session, automation)
    if isinstance(child, Cron):
        from app.routes.crons import write_cron_delete

        write_cron_delete(session, child, actor, background)
    else:
        from app.routes.triggers import write_trigger_delete

        write_trigger_delete(session, child, actor)


def release_run(session: Session, automation: Automation, *, actor: "AdminUser") -> bool:
    """End a stuck schedule's held run lock (`cron_runtime.release_run_lock`), keeping its run history. 409 for
    an event automation, which has no run lock, and for a schedule that is not stuck (`stuck_run`), re-read here
    so a stale page or a direct call never ends a healthy firing. True iff a holder was ended."""
    from app import cron_runtime

    child = child_of(session, automation)
    if not isinstance(child, Cron):
        raise HTTPException(status_code=409, detail="only a schedule has a run to release")
    if stuck_run(session, child, newest_run(session, child)) is None:
        raise HTTPException(status_code=409, detail="not stuck: no run to release")
    return cron_runtime.release_run_lock(child.id, by=actor.id)


def set_paused(session: Session, automation: Automation, paused: bool, *, actor: "AdminUser", background: Any,
               reason: str = "") -> None:
    """The one `paused` control over the child's own pause (§6.3)."""
    child = child_of(session, automation)
    if isinstance(child, Cron):
        from app.routes.crons import CronUpdate, write_cron_update

        locked = session.scalar(select(Cron).where(Cron.id == child.id).with_for_update())
        write_cron_update(session, locked, CronUpdate(enabled=not paused), actor, background)
        return
    from app.routes.triggers import write_pause, write_resume

    locked = session.scalar(select(TaskTrigger).where(TaskTrigger.id == child.id).with_for_update())
    if paused:
        write_pause(session, locked, actor, reason)
    else:
        write_resume(session, locked, actor)
