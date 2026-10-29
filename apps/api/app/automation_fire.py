"""The fire engine — one fan-out for every way an automation runs: a schedule tick
(`cron_runtime`), an event delivery (`trigger_intake`) and Run now (`routes/automations.py`).

`fire(session, automation, cause=…)` resolves the automation's live Target rule (`target_resolver.resolve`, D3) and
hands each target to the action's per-target filer (`ActionDescriptor.fire`; the Task action's is `file_task_target`
below). Filing commits inside `routes/tasks/dispatch.py::_start`/`_start_epic`, so savepoints cannot span targets; instead the
engine is ONE COMMIT PER TARGET (§4.2):

1. the run is claimed up front and committed — a schedule's `cron_runs` row is written provisional (`acted`, no
   `finished_at`) by `cron_runtime`; an event's delivery moves to `filing` under a `claimed_until` lease;
2. targets are resolved once;
3. per target: the deadline (`timeout − DEADLINE_SLACK_SECONDS`; past it a schedule records every remaining target
   `not_reached`, and an event owes them on the delivery for the next intake pass, `REASON_DEFERRED`),
   overlap (a schedule skips a target whose previous task is still open, D10) or the concurrency cap (an event
   queues a target at its cap, owed on the delivery), the owner's authority on that target, then the filer — the
   task, its `automation_run_targets` row and its provenance in ONE commit;
4. the run is finalised by precedence: ≥1 `filed` → acted; else ≥1 `error` → error; else noop.

A later target's error never undoes an earlier filing, and an event's dedup reservation is released only when no
target filed — so a redelivery can never re-file a target that already got its task. A crashed event firing is
resumed for the targets with no ledger row only (`trigger_intake`'s stale-claim path), and a crashed schedule's
provisional row is closed "Interrupted" at the next tick (`close_interrupted_runs`) — one that outran its bound is
settled in place by the cron wrapper's timeout instead.

An action with no Target (Send email, `app/automation_email.py`) runs once per firing (`_fire_untargeted`) and writes no
ledger row: its one outcome is the run record's own detail (a schedule's `cron_runs`, an event's delivery reason).

Every sentence a target can be recorded with is in `REASONS` (a closed set; the templated ones name their slot).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Optional
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.automations import (
    MODE_SPECIFIC,
    OUTCOME_ERROR,
    OUTCOME_FILED,
    OUTCOME_NOT_REACHED,
    OUTCOME_QUEUED,
    OUTCOME_SKIPPED,
    TARGET_NODES,
    TARGET_PROJECTS,
)
from app.clock import utcnow as _now
from app.entity_shape import optional_uuid, readable_error
from app.models.automations import Automation, AutomationRunTarget, CronRun, TaskTrigger, TriggerDedup, TriggerDelivery
from app.models.tasks import Task
from app.target_resolver import REASON_NODE_DISABLED, TargetItem, target_key
from app.task_ancestry import may_stamp_starter
from app.task_lifecycle import _EPIC_SETTLED_STATES, AWAITING_RESPONSE, DRAFT

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.authz.principal import Principal
    from app.models.identity import AdminUser
    from app.models.projects import Project
    from app import task_templates

log = logging.getLogger("automation-fire")

Cause = Literal["schedule", "event", "manual"]
CAUSE_SCHEDULE: Cause = "schedule"
CAUSE_EVENT: Cause = "event"
CAUSE_MANUAL: Cause = "manual"
CAUSES: tuple[str, ...] = (CAUSE_SCHEDULE, CAUSE_EVENT, CAUSE_MANUAL)

#: States in which a target's previous task no longer blocks the next firing there (D10, schedule overlap).
#: `awaiting_response` does not block: one unanswered question must not stall the schedule — safe only because a
#: Full Auto task asks only about human-only blockers and parks are bounded
#: (`task_pipeline.expire_timed_out_awaiting_response_tasks`). `paused` still blocks: the operator's deliberate hold.
#: `draft` does not block: a Task action left as a draft (`start: false`) files tasks for a person to start, so an
#: unstarted one must not skip every later firing (nor count toward an event's cap, `open_in_target`).
NOT_BLOCKING_STATES: frozenset[str] = frozenset({*_EPIC_SETTLED_STATES, AWAITING_RESPONSE, DRAFT})

#: The engine stops this long before its bound, so it records `not_reached` itself rather than being abandoned
#: mid-target by `cron_runtime._bounded_call` (critic finding 3).
DEADLINE_SLACK_SECONDS = 15.0

#: The marker a Run now's run record carries at the head of its detail (no cause column on `cron_runs` or
#: `trigger_deliveries` this release); the Runs view reads it back as cause `manual` (`cause_of`).
MANUAL_MARK = "Run now · "

# ── The closed set of per-target reasons (`automation_run_targets.reason`) ────────────────────────────
REASON_PREVIOUS_OPEN = "previous still open"
REASON_NODE_GONE = "node deleted or no longer in this org"
REASON_PROJECT_GONE = "project deleted or no longer in this org"
REASON_TIMEOUT = "timeout before this target"
REASON_QUEUE_FULL = "queue full"
REASON_NO_LONGER_TARGETED = "no longer targeted by this automation"
REASON_OWNER_GONE = "the automation's owner no longer exists — re-own it"
REASON_OWNER_OTHER_ORG = "the automation's owner is in another organization — re-own it"
REASON_OWNER_LACKS = "owner lacks {key}"
REASON_OWNER_NOT_ADMIN = "the automation's owner is no longer an org admin — re-own it"
REASON_KEY_GONE = "the API key it arrived with no longer works (revoked, expired, deleted, or its owner lost it)"
REASON_OWNER_NO_ACCESS = "owner has no access to {target}"
REASON_ASSIGNEE_UNREACHABLE = "assignee not a member of {target}; filed unassigned"
REASON_WAITING = "waiting: {running} of {cap} filed Tasks still running"
REASON_FILING_FAILED = "filing failed: {error}"
REASON_SEND_FAILED = "email not sent: {error}"
#: A resumed event firing of an action with no Target: it may already have sent, so it is never sent twice.
REASON_INTERRUPTED_UNSENT = "interrupted mid-send; not retried, so it is never sent twice"
#: An event target the firing ran out of time for: owed on the delivery, filed by a later intake pass.
REASON_DEFERRED = "deferred: this pass ran out of time; the next one files it"
#: The run-level detail of a rule that resolved to nothing.
REASON_NO_TARGETS = "No targets matched this rule"
#: A Task action filed from a template whose row is gone, or no longer startable here (unpublished, switched off).
REASON_TEMPLATE_DELETED = "Template deleted"
REASON_TEMPLATE_UNAVAILABLE = "Template unavailable"
REASONS: tuple[str, ...] = (
    REASON_PREVIOUS_OPEN, REASON_NODE_DISABLED, REASON_NODE_GONE, REASON_PROJECT_GONE, REASON_TIMEOUT,
    REASON_QUEUE_FULL, REASON_NO_LONGER_TARGETED, REASON_OWNER_GONE, REASON_OWNER_OTHER_ORG, REASON_OWNER_LACKS,
    REASON_OWNER_NO_ACCESS, REASON_ASSIGNEE_UNREACHABLE, REASON_WAITING, REASON_FILING_FAILED, REASON_DEFERRED,
    REASON_NO_TARGETS, REASON_OWNER_NOT_ADMIN, REASON_SEND_FAILED, REASON_INTERRUPTED_UNSENT,
    REASON_TEMPLATE_DELETED, REASON_TEMPLATE_UNAVAILABLE,
)
#: A target refused for one of these is the owner's authority, not the target's fault (an event marks its trigger
#: failed when every target was refused this way — today's single-project behaviour).
_AUTHORITY_PREFIXES = ("owner lacks", "owner has no access", "the automation's owner")


class TargetFailed(Exception):
    """This target cannot be filed, for the operator-readable `reason`; recorded as `outcome`, the others go on."""

    def __init__(self, reason: str, outcome: str = OUTCOME_ERROR):
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


@dataclass(frozen=True)
class TargetOutcome:
    outcome: str
    reason: str = ""
    task_id: Optional[int] = None
    missing_paths: tuple[str, ...] = ()


@dataclass
class FireContext:
    """What a per-target filer is handed: the automation as it stands, its validated params, the firing's cause and
    run record, the owner it files as, and (events) the event its templates render."""

    session: Session
    automation: Automation
    params: dict[str, Any]
    cause: str
    owner: "AdminUser"
    principal: "Principal"
    run_column: str  # "cron_run_id" | "delivery_id"
    run_id: Any
    event: Optional[dict[str, Any]] = None
    renders: bool = False
    # The event source kind (`task_source.trigger_source`); None for a schedule (`task_source.CRON`).
    trigger_kind: Optional[str] = None
    # When this firing was due (a schedule's `fired_at`) or arrived (an event's `received_at`).
    fired_at: Optional[datetime] = None
    # The API key a webhook delivery arrived with, whose owner is `owner` (`key_filer`); None for every other firing.
    api_key: Optional["KeyFiler"] = None

    def __post_init__(self) -> None:
        # Scalars captured while fresh: a per-target rollback expires the ORM rows (see `_reload_owner`).
        self.owner_id = self.owner.id if self.owner is not None else None
        self.automation_id = self.automation.id


@dataclass(frozen=True)
class KeyFiler:
    """The owner of the API key a webhook delivery arrived with, their principal narrowed to the key, and the key."""

    owner: "AdminUser"
    principal: "Principal"
    key_id: UUID
    key_name: str


@dataclass
class FireResult:
    """One firing's verdict. `outcome` is the run's (`acted`/`error`/`noop`); `state`/`reason` an event delivery's
    final state and reason; `task_ids` every task this call filed, first first."""

    outcome: str
    detail: str
    task_ids: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    state: Optional[str] = None
    reason: Optional[str] = None
    # A schedule whose every target was skipped "previous still open": the whole firing is that skip, as the
    # Runner's always was, so the wedged-cron alarm still counts it (`cron_runtime._run_fire`).
    all_previous_open: bool = False


# ── Small reads ───────────────────────────────────────────────────────────────────────────────────────


def cause_of(detail: Optional[str], default: str) -> str:
    """A run record's cause, read back from its detail (see `MANUAL_MARK`)."""
    return CAUSE_MANUAL if (detail or "").startswith(MANUAL_MARK) else default


def _key(item: TargetItem) -> str:
    return target_key(item.kind, item.id)


def _task_scope(automation_id: UUID, kind: str, target_id: Any):
    """The tasks this automation filed IN this target: same project (projects) or same pin (nodes)."""
    column = Task.pinned_node_id if kind == TARGET_NODES else Task.project_id
    value = UUID(str(target_id)) if kind == TARGET_NODES else int(target_id)
    return (Task.created_by_automation_id == automation_id) & (column == value)


def previous_still_open(session: Session, automation_id: UUID, kind: str, target_id: Any) -> bool:
    """D10: is this automation's newest task in this target still open? `id` DESC — two firings can share an
    instant; the pk is the only total order."""
    state = session.scalar(
        select(Task.state).where(_task_scope(automation_id, kind, target_id)).order_by(Task.id.desc()).limit(1)
    )
    return state is not None and state not in NOT_BLOCKING_STATES


def open_in_target(session: Session, automation_id: UUID, kind: str, target_id: Any) -> int:
    """An event's concurrency count in one target: this automation's tasks there not yet settled (the trigger
    cap's own set, `trigger_intake`), less the drafts nobody has started (see NOT_BLOCKING_STATES)."""
    return session.scalar(
        select(func.count()).select_from(Task).where(
            _task_scope(automation_id, kind, target_id), Task.state.not_in((*_EPIC_SETTLED_STATES, DRAFT))
        )
    ) or 0


def _last_known_name(session: Session, automation_id: UUID, kind: str, key: str) -> str:
    """A dangling id's label: the name a past run snapshotted for it, else `#<id>`."""
    name = session.scalar(
        select(AutomationRunTarget.target_name).where(
            AutomationRunTarget.automation_id == automation_id, AutomationRunTarget.target_kind == kind,
            AutomationRunTarget.target_id == key, AutomationRunTarget.target_name != "",
        ).order_by(AutomationRunTarget.id.desc()).limit(1)
    )
    return name or f"#{key}"


# ── The ledger ────────────────────────────────────────────────────────────────────────────────────────


def _ledger_row(ctx_run_column: str, run_id: Any, automation: Automation, kind: str, key: str, name: str,
                outcome: str, reason: str, task_id: Optional[int] = None) -> AutomationRunTarget:
    return AutomationRunTarget(
        org_id=automation.org_id, automation_id=automation.id, target_kind=kind, target_id=key,
        target_name=name or "", outcome=outcome, reason=reason or "", task_id=task_id,
        **{ctx_run_column: run_id},
    )


def run_targets(session: Session, column: str, run_id: Any) -> list[AutomationRunTarget]:
    run_col = AutomationRunTarget.cron_run_id if column == "cron_run_id" else AutomationRunTarget.delivery_id
    return list(session.scalars(
        select(AutomationRunTarget).where(run_col == run_id).order_by(AutomationRunTarget.id)
    ))


def _record(session: Session, ctx: FireContext, item: TargetItem, outcome: TargetOutcome) -> None:
    """Write (or, for an owed target, update) this target's ledger row, and commit."""
    existing = session.scalar(select(AutomationRunTarget).where(
        getattr(AutomationRunTarget, ctx.run_column) == ctx.run_id,
        AutomationRunTarget.target_kind == item.kind, AutomationRunTarget.target_id == _key(item),
    ))
    if existing is None:
        session.add(_ledger_row(ctx.run_column, ctx.run_id, ctx.automation, item.kind, _key(item), item.name,
                                outcome.outcome, outcome.reason, outcome.task_id))
    else:
        existing.outcome, existing.reason, existing.task_id = outcome.outcome, outcome.reason, outcome.task_id
    session.commit()


# ── The owner ─────────────────────────────────────────────────────────────────────────────────────────


def key_filer(session: Session, key_id: UUID, org_id: UUID) -> KeyFiler | str:
    """Who files a webhook delivery that arrived with API key `key_id`: its owner, narrowed to the key as any API
    request is (`authz.principal.key_principal`), while the key still works and is `org_id`'s; else the reason."""
    from app import api_keys
    from app.authz.principal import RevokedSession, key_principal

    context = api_keys.context_of(key_id, org_id)
    if isinstance(context, api_keys.Refusal) or context.org_id != org_id:
        return REASON_KEY_GONE
    try:
        principal = key_principal(context)
    except RevokedSession:
        return REASON_KEY_GONE
    owner = api_keys.owner_row(session, context.owner_id)
    if owner is None:
        return REASON_KEY_GONE
    return KeyFiler(owner=owner, principal=principal, key_id=context.key_id, key_name=context.name)


def _owner_or_reason(
    session: Session, automation: Automation, *, files_tasks: bool = True, filer: Optional[KeyFiler] = None,
) -> tuple[Optional["AdminUser"], Optional["Principal"], Optional[str]]:
    """The owner as whom every task is filed (or the email sent) and their real principal, or the reason the firing
    may not act: the automation's owner, or an API key's (`filer`, already held to the key's org). Asked once per
    firing against live grants: a Task action needs `tasks:create` (files) and `tasks:edit` (starts); an action that
    files nothing needs the owner still an org admin, as saving it did."""
    from app.authz.catalog import permission_key
    from app.authz.principal import principal_for_user
    from app.models.identity import AdminUser
    from app.tenancy import seam

    if filer is not None:
        owner, principal = filer.owner, filer.principal
    else:
        # Resolved as a caller (`seam.identity_scope`): under the automation's org bind an owner elsewhere reads
        # missing.
        with seam.identity_scope():
            owner = session.scalar(select(AdminUser).where(AdminUser.id == automation.created_by_id))
        if owner is None or owner.is_system:
            return None, None, REASON_OWNER_GONE
        if owner.org_id is not None and owner.org_id != automation.org_id:
            return None, None, REASON_OWNER_OTHER_ORG
        principal = principal_for_user(session, owner)
    if not files_tasks:
        if not (principal.is_org_admin or principal.is_operator):
            return None, None, REASON_OWNER_NOT_ADMIN
        return owner, principal, None
    for key in (permission_key("tasks", "create"), permission_key("tasks", "edit")):
        if not principal.has(key):
            return None, None, REASON_OWNER_LACKS.format(key=key)
    return owner, principal, None


# ── The Task action's per-target filer (`automation_actions.TASK_ACTION.fire`) ────────────────────────


def file_task_target(ctx: FireContext, item: TargetItem) -> TargetOutcome:
    """File one task for one target and start it, unless the action leaves it a draft. Everything is flushed, then `_start`/`_start_epic` commits the task,
    its ledger row and its provenance together. Raises `TargetFailed` for a refusal the operator can act on."""
    from fastapi import HTTPException

    from app import actors, node_targets
    from app.authz.object_access import can_access
    from app.authz.visibility import ObjectAccess
    from app.events import emit_console_event
    from app.models.projects import Project
    from app.routes.tasks.policy import PERSONA_EPIC
    from app.routes.tasks.create import _create_epic_children, _link_template, _validated_assignee
    from app.routes.tasks.dispatch import _start, _start_epic

    session, automation, params = ctx.session, ctx.automation, ctx.params
    pinned: Optional[UUID] = None
    if item.kind == TARGET_NODES:
        pinned = UUID(str(item.id))
        project_id = node_targets.prepare(session, automation.org_id, pinned)
    else:
        project_id = int(item.id)
    project = session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise TargetFailed(REASON_PROJECT_GONE)
    label = item.name or project.name
    if not can_access(session, ctx.principal, "project", project.id, mode=ObjectAccess.WRITE):
        raise TargetFailed(REASON_OWNER_NO_ACCESS.format(target=label), OUTCOME_SKIPPED)

    template = _live_template(ctx)
    body, title, missing = _render(ctx, template)
    reason = ""
    assignee: Optional[UUID] = None
    if params.get("assignee_id"):
        try:
            assignee = _validated_assignee(session, UUID(str(params["assignee_id"])), project_id=project.id,
                                           actor=ctx.owner)
        except HTTPException:
            reason = REASON_ASSIGNEE_UNREACHABLE.format(target=label)
            assignee = None
    feature_id = _feature_for(session, params, project.id)
    try:
        task = _build_task(ctx, project, body=body, title=title, pinned=pinned, assignee=assignee,
                           feature_id=feature_id, template=template)
    except HTTPException as exc:
        raise TargetFailed(REASON_FILING_FAILED.format(error=readable_error(exc))) from exc
    epic = task.persona == PERSONA_EPIC
    actors.set_request_actor(session, ctx.owner_id)
    session.info[actors.MACHINE_ACT_SESSION_KEY] = True
    try:
        session.add(task)
        session.flush()
        if epic:
            _create_epic_children(task, session)
        _copy_attachments(session, automation, params, task)
        # The link, the name kept past a delete and the images its body names; never a start (I-2: no person).
        _link_template(session, task, template, None)
        # An owed target's queued row is replaced by its filed one, in the same commit.
        session.execute(delete(AutomationRunTarget).where(
            getattr(AutomationRunTarget, ctx.run_column) == ctx.run_id,
            AutomationRunTarget.target_kind == item.kind, AutomationRunTarget.target_id == _key(item),
            AutomationRunTarget.outcome == OUTCOME_QUEUED,
        ))
        session.add(_ledger_row(ctx.run_column, ctx.run_id, automation, item.kind, _key(item), item.name,
                                OUTCOME_FILED, reason, task.id))
        emit_console_event(session, entity="task", id=task.id, action="created")
        task_id = task.id
        if not params.get("start", True):
            # Left as a draft (the action's Start: Leave as draft): committed with its ledger row, never dispatched.
            session.commit()
        elif epic:
            _start_epic(task_id, project, session, None)
        elif not _start(task_id, session, sources=(DRAFT,), run_requested=False):
            raise TargetFailed(REASON_FILING_FAILED.format(error=f"Task #{task_id} was created but would not start"))
    except HTTPException as exc:
        raise TargetFailed(REASON_FILING_FAILED.format(error=readable_error(exc))) from exc
    finally:
        actors.set_request_actor(session, None)
        session.info.pop(actors.MACHINE_ACT_SESSION_KEY, None)
    if template is None and not (ctx.renders and params.get("title_template")):
        from app.durable.task_title_queue import enqueue_title

        enqueue_title(task_id)
    if assignee is not None and assignee != ctx.owner_id:
        from app import task_notify

        task_notify.announce_assignment(task_id, assignee_id=assignee, assigned_by_id=ctx.owner_id)
    return TargetOutcome(OUTCOME_FILED, reason, task_id, tuple(missing))


def _live_template(ctx: FireContext) -> Optional["task_templates.AnyTemplate"]:
    """The Task action's template as it is NOW (D5, live reference), or None for a body action. Raises `TargetFailed`
    rather than file a blank task: `REASON_TEMPLATE_DELETED` when no row is left, `REASON_TEMPLATE_UNAVAILABLE` when
    it no longer starts in the automation's org (unpublished, or its org's "Templates (org)" switch is off)."""
    from app import task_templates

    template_id = optional_uuid(ctx.params.get("template_id"))
    if template_id is None:
        return None
    if task_templates.find(ctx.session, template_id) is None:
        raise TargetFailed(REASON_TEMPLATE_DELETED)
    from app.persona_registry import CREATABLE_PERSONAS

    template = task_templates.startable(ctx.session, template_id, ctx.automation.org_id)
    if template is None or not (template.body or "").strip() or template.persona not in CREATABLE_PERSONAS:
        raise TargetFailed(REASON_TEMPLATE_UNAVAILABLE)
    return template


def _render(ctx: FireContext, template: Optional["task_templates.AnyTemplate"] = None) -> tuple[str, str, list[str]]:
    """(body, title, missing paths). A schedule files its body literally and titles it from the body (the LLM title
    follows via the enqueue); an event renders its templates over the event, once per target. A template's body is
    filed literally (no placeholders in template text) under the template's title, or an event's `title_template`."""
    from app.task_title import placeholder_title

    params = ctx.params
    if template is not None:
        body = template.body.strip()
        title = " ".join((template.title or "").split()) or placeholder_title(_unbroken(body))
        if not (ctx.renders and (params.get("title_template") or "").strip()):
            return body, title, []
        from app.trigger_template import render_title

        rendered_title = render_title(params["title_template"], ctx.event or {})
        return body, rendered_title.text or title, list(rendered_title.missing_paths)
    if not ctx.renders:
        body = str(params["body"]).strip()
        if not body:
            raise TargetFailed(REASON_FILING_FAILED.format(error="the Task action's body is blank"))
        return body, placeholder_title(_unbroken(body)), []
    from app.trigger_template import render, render_title

    event = ctx.event or {}
    rendered = render(params["body"], event)
    missing = list(rendered.missing_paths)
    if (params.get("title_template") or "").strip():
        title_rendered = render_title(params["title_template"], event)
        missing += [p for p in title_rendered.missing_paths if p not in missing]
        title = title_rendered.text or placeholder_title(_unbroken(rendered.text))
    else:
        title = placeholder_title(_unbroken(rendered.text))
    if not rendered.text.strip():
        raise TargetFailed(REASON_FILING_FAILED.format(error="the prompt template rendered an empty body"))
    return rendered.text, title, missing


def _unbroken(body: str) -> str:
    """The body with its markdown line breaks (`\\` + newline, the automation editor's Enter) as plain newlines, so a
    title drawn from it carries no stray backslash. The body itself keeps them: the task page renders them as lines."""
    return body.replace("\\\n", "\n")


def _feature_for(session: Session, params: dict[str, Any], project_id: int) -> Optional[UUID]:
    """The Task action's feature, while it is still an open feature of this project (it was checked at save; a
    shipped or moved feature files the task without it rather than failing the firing)."""
    from app.features import FEATURE_SHIPPED
    from app.models.projects import Feature

    if not params.get("feature_id"):
        return None
    feature = session.scalar(select(Feature).where(Feature.id == UUID(str(params["feature_id"]))))
    if feature is None or feature.project_id != project_id or feature.status == FEATURE_SHIPPED:
        return None
    return feature.id


def _build_task(ctx: FireContext, project: "Project", *, body: str, title: str, pinned: Optional[UUID],
                assignee: Optional[UUID], feature_id: Optional[UUID],
                template: Optional["task_templates.AnyTemplate"] = None) -> Task:
    """The row, as the create route builds it from the same spec (`routes/tasks/create.py::_create_coder_task` /
    `_create_epic_with_children`), plus the automation's provenance and the child's (`created_by_cron_*` /
    `created_by_trigger_*`: git authority, the budget flag and the Task page read them). A template supplies the
    persona, autonomy and model pin (its editor's, validated but never re-authorized, as on Start)."""
    from app import task_source
    from app.routes.tasks.policy import DEFAULT_TRANSITION_MODE, PERSONA_EPIC
    from app.routes.tasks.create import _provider_on_plan_or_422, _playbook_plan_or_422, new_top_level_task

    params, automation = ctx.params, ctx.automation
    persona = params["persona"]
    model = params.get("model") or None
    autonomous = bool(params.get("autonomous", True))
    if template is not None:
        from app import model_catalog

        persona, autonomous = template.persona, bool(template.autonomous)
        try:
            model = model_catalog.validate_model(template.model) or None
        except ValueError as exc:
            raise TargetFailed(REASON_FILING_FAILED.format(error=f"the template's model: {exc}")) from exc
    plan = None
    if params.get("playbook_id"):
        plan = _playbook_plan_or_422(ctx.session, project, UUID(str(params["playbook_id"])), persona, model)
        persona = plan.persona
    if persona != PERSONA_EPIC:
        # A plan turned off since save refuses the pin, as a refused playbook does, not a task no account can run.
        _provider_on_plan_or_422(ctx.session, params.get("bound_provider") or None, project.org_id)
    task = new_top_level_task(
        project_id=project.id,
        source=task_source.CRON,
        title=title,
        body=body,
        persona=persona,
        created_by_id=ctx.owner_id,
        priority=params.get("priority"),
        autonomous=autonomous,
        transition_mode=params.get("transition_mode") or DEFAULT_TRANSITION_MODE,
        model=model,
        hidden=bool(params.get("hidden", False)),
        assigned_to_id=assignee,
        feature_id=feature_id,
        pinned_node_id=pinned,
    )
    if persona == PERSONA_EPIC:
        task.is_plannable = True
        if plan is not None:
            task.playbook = plan.snapshot
    else:
        task.bound_provider = params.get("bound_provider") or None
        if plan is not None:
            task.playbook, task.playbook_stage = plan.snapshot, plan.stage
    if ctx.trigger_kind is not None:
        # An event's task carries its source kind, as a trigger's always has (`trigger_source` refuses an unknown).
        task.source = task_source.trigger_source(ctx.trigger_kind)
    task.project_memory = params.get("project_memory")
    # The owner started it: an automation files under the licence of the person who owns it, so the subtree pushes
    # with their git access. Only a seat in the automation's org (`task_ancestry.may_stamp_starter`).
    task.started_by_id = ctx.owner_id if may_stamp_starter(ctx.owner, automation.org_id) else None
    task.org_id = automation.org_id
    task.created_by_automation_id = automation.id
    task.created_by_automation_name = automation.name
    if ctx.api_key is not None:
        task.created_by_api_key_id, task.created_by_api_key_name = ctx.api_key.key_id, ctx.api_key.key_name
    child = _legacy_child(ctx)
    if child is not None:
        if ctx.run_column == "cron_run_id":
            task.created_by_cron_id, task.created_by_cron_name = child.id, child.name
        else:
            task.created_by_trigger_id, task.created_by_trigger_name = child.id, child.name
    return task


def _legacy_child(ctx: FireContext):
    from app.models.automations import Cron

    model = Cron if ctx.run_column == "cron_run_id" else TaskTrigger
    return ctx.session.scalar(select(model).where(model.automation_id == ctx.automation_id))


def _copy_attachments(session: Session, automation: Automation, params: dict[str, Any], task: Task) -> None:
    """Each of the Task action's files onto the fired task, as a row of its own sharing the object (`s3_key`): the
    bytes are stored once, and every delete path reaps a key only once no row names it
    (`app/attachment_storage.py::reap_unreferenced_object`)."""
    from app.models.task_extras import TaskAttachment

    ids = [int(i) for i in params.get("attachment_ids") or []]
    if not ids:
        return
    sources = session.scalars(select(TaskAttachment).where(
        TaskAttachment.id.in_(ids), TaskAttachment.automation_id == automation.id,
        TaskAttachment.uploaded_at.is_not(None),
    # Locked to this commit, so a concurrent delete of the source cannot reap the object before the copy names it.
    ).order_by(TaskAttachment.id).with_for_update()).all()
    for source in sources:
        session.add(TaskAttachment(
            org_id=task.org_id, task_id=task.id, kind=source.kind, filename=source.filename,
            content_type=source.content_type, size_bytes=source.size_bytes, s3_key=source.s3_key,
            s3_bucket=source.s3_bucket, etag=source.etag,
            uploaded_at=source.uploaded_at, uploaded_by_id=source.uploaded_by_id, visibility=source.visibility,
        ))


# ── The fan-out ───────────────────────────────────────────────────────────────────────────────────────


def fire(
    session: Session,
    automation: Automation,
    *,
    cause: Cause,
    cron_run: Optional[CronRun] = None,
    delivery: Optional[TriggerDelivery] = None,
    event: Optional[dict[str, Any]] = None,
    timeout_seconds: Optional[float] = None,
) -> FireResult:
    """Fire `automation` once. Exactly one of `cron_run` (a schedule's provisional run record, already committed)
    and `delivery` (an event's, or an event automation's Run now) names the run. `session` must be bound to the
    automation's org. Commits per target; never leaves an open transaction behind."""
    from app import automation_actions, target_resolver

    if (cron_run is None) == (delivery is None):
        raise ValueError("automation_fire.fire: exactly one of cron_run and delivery names the run")
    descriptor = automation_actions.get(automation.action_type)
    if descriptor.fire is None:
        raise ValueError(f"automation_fire.fire: action {automation.action_type!r} has no per-target filer")
    bound = float(descriptor.timeout_seconds if timeout_seconds is None else timeout_seconds)
    # A bound at or under the slack leaves no time: every target is recorded `not_reached` rather than raced.
    deadline = time.monotonic() + bound - DEADLINE_SLACK_SECONDS
    automation_id = automation.id
    params = (automation_actions.task_params(automation.action_params)
              if automation.action_type == automation_actions.ACTION_TASK else dict(automation.action_params or {}))
    is_event = delivery is not None
    trigger_kind = None
    if is_event:
        trigger_kind = session.scalar(select(TaskTrigger.source_kind).where(TaskTrigger.automation_id == automation_id))
    run_column, run_id = ("delivery_id", delivery.id) if is_event else ("cron_run_id", cron_run.id)

    resolution = target_resolver.resolve(session, automation)
    keyed = (key_filer(session, delivery.api_key_id, automation.org_id)
             if delivery is not None and delivery.api_key_id is not None else None)
    filer = keyed if isinstance(keyed, KeyFiler) else None
    owner, principal, refusal = (
        (None, None, keyed) if isinstance(keyed, str)
        else _owner_or_reason(session, automation, files_tasks=descriptor.takes_target, filer=filer)
    )
    ctx = FireContext(
        session=session, automation=automation, params=params, cause=cause, owner=owner, principal=principal,
        run_column=run_column, run_id=run_id, event=event, renders=is_event, trigger_kind=trigger_kind,
        fired_at=delivery.received_at if delivery is not None else cron_run.fired_at if cron_run is not None else None,
        api_key=filer,
    )
    if not descriptor.takes_target:
        return _fire_untargeted(ctx, descriptor, delivery, refusal, bound)
    if is_event:
        return _fire_event(ctx, descriptor, resolution, delivery, refusal, deadline, bound)
    return _fire_schedule(ctx, descriptor, resolution, refusal, deadline)


def _fire_untargeted(ctx: FireContext, descriptor, delivery: Optional[TriggerDelivery], refusal: Optional[str],
                     bound: float) -> FireResult:
    """An action with no Target: its filer runs once, and its outcome is the run's (acted when it did its thing, else
    error). A schedule returns it for `cron_runtime` to record; an event settles its delivery here."""
    from app.crons import RUN_OUTCOME_ACTED, RUN_OUTCOME_ERROR
    from app.triggers import FILING

    session = ctx.session
    if delivery is not None:
        if delivery.state == FILING and ctx.cause != CAUSE_MANUAL:
            # A crashed firing resumed (a Run now's delivery is born `filing`): it may have sent before it died, and
            # a message is never sent twice.
            return _settle_untargeted_delivery(ctx, delivery, TargetOutcome(OUTCOME_ERROR, REASON_INTERRUPTED_UNSENT))
        delivery.state = FILING
        # Never shorter than the action's own bound: a slow send must not look abandoned to another pass.
        delivery.claimed_until = _now() + timedelta(
            seconds=max(bound, float(descriptor.timeout_seconds), DEADLINE_SLACK_SECONDS))
        session.commit()
    if refusal is not None:
        outcome = TargetOutcome(OUTCOME_ERROR, refusal)
    else:
        try:
            outcome = descriptor.fire(ctx, None)
        except TargetFailed as failed:
            session.rollback()
            outcome = TargetOutcome(failed.outcome, failed.reason)
        except Exception as exc:  # noqa: BLE001 — the firing's fault is its run's error, recorded like any other
            log.exception("automation %s: the %s action failed", ctx.automation_id, descriptor.type)
            session.rollback()
            outcome = TargetOutcome(OUTCOME_ERROR, REASON_SEND_FAILED.format(error=readable_error(exc)))
    if delivery is not None:
        return _settle_untargeted_delivery(ctx, delivery, outcome)
    acted = outcome.outcome == OUTCOME_FILED
    detail = outcome.reason
    if outcome.missing_paths:
        detail += f" — empty: {', '.join(outcome.missing_paths)}"
    if ctx.cause == CAUSE_MANUAL:
        detail = MANUAL_MARK + detail
    return FireResult(outcome=RUN_OUTCOME_ACTED if acted else RUN_OUTCOME_ERROR, detail=detail)


def _settle_untargeted_delivery(ctx: FireContext, delivery: TriggerDelivery, outcome: TargetOutcome) -> FireResult:
    """An event delivery of an action with no Target, closed from its one outcome — one commit."""
    from app.triggers import DISPATCHED, FAILED

    session = ctx.session
    delivery = session.scalar(select(TriggerDelivery).where(TriggerDelivery.id == delivery.id))
    trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == delivery.trigger_id).with_for_update())
    acted = outcome.outcome == OUTCOME_FILED
    state = DISPATCHED if acted else FAILED
    reason = outcome.reason
    if outcome.missing_paths:
        reason += f" — empty: {', '.join(outcome.missing_paths)}"
        delivery.missing_paths = list(outcome.missing_paths)
    if ctx.cause == CAUSE_MANUAL:
        reason = MANUAL_MARK + reason
    delivery.state, delivery.reason, delivery.processed_at = state, reason, _now()
    delivery.claimed_until, delivery.owed_targets, delivery.task_id = None, None, None
    _close_delivery(session, delivery, trigger, state, reason, acted=acted, first_task=None,
                    authority_reason=outcome.reason if outcome.reason.startswith(_AUTHORITY_PREFIXES) else None)
    session.commit()
    return FireResult(outcome="acted" if acted else "error", detail=reason, state=state, reason=reason)


def _side_rows(ctx: FireContext, resolution) -> list[tuple[TargetItem, TargetOutcome]]:
    """The targets the rule names but no filer will see: disabled nodes (skipped), and — for a Specific rule — ids
    that left the universe: a node is skipped (AC2.2), a project is an error the operator must fix (today's Runner
    and trigger both fail on it)."""
    session, automation = ctx.session, ctx.automation
    out: list[tuple[TargetItem, TargetOutcome]] = [
        (item, TargetOutcome(OUTCOME_SKIPPED, reason)) for item, reason in resolution.skipped
    ]
    if automation.target_mode != MODE_SPECIFIC:
        return out
    for key in resolution.dangling:
        name = _last_known_name(session, automation.id, automation.target_kind, key)
        if automation.target_kind == TARGET_NODES:
            item, outcome = TargetItem(kind=TARGET_NODES, id=key, name=name), TargetOutcome(OUTCOME_SKIPPED, REASON_NODE_GONE)
            out.append((item, outcome))
        else:
            out.append((TargetItem(kind=TARGET_PROJECTS, id=key, name=name),
                        TargetOutcome(OUTCOME_ERROR, REASON_PROJECT_GONE)))
    return out


def _file_one(ctx: FireContext, descriptor, item: TargetItem) -> TargetOutcome:
    """Run the filer for one target; a refusal or a fault becomes this target's ledger row, committed."""
    session = ctx.session
    try:
        outcome = descriptor.fire(ctx, item)
    except TargetFailed as failed:
        session.rollback()
        _reload_owner(ctx)
        outcome = _already_filed(ctx, item) or TargetOutcome(failed.outcome, failed.reason)
        if outcome.outcome != OUTCOME_FILED:
            _record(session, ctx, item, outcome)
        return outcome
    except Exception as exc:  # noqa: BLE001 — one target's fault is that target's error, never the firing's
        log.exception("automation %s: target %s %s failed", ctx.automation_id, item.kind, item.id)
        session.rollback()
        _reload_owner(ctx)
        outcome = _already_filed(ctx, item) or TargetOutcome(
            OUTCOME_ERROR, REASON_FILING_FAILED.format(error=readable_error(exc)))
        if outcome.outcome != OUTCOME_FILED:
            _record(session, ctx, item, outcome)
        return outcome
    return outcome


def _reload_owner(ctx: FireContext) -> None:
    """A rollback expires the owner, which was read under the identity escape; re-read it there, so the next
    target never lazy-loads it under the automation's org bind (an owner elsewhere would read as gone)."""
    from app.models.identity import AdminUser
    from app.tenancy import seam

    if ctx.owner_id is None:
        return
    with seam.identity_scope():
        ctx.owner = ctx.session.scalar(select(AdminUser).where(AdminUser.id == ctx.owner_id))


def _already_filed(ctx: FireContext, item: TargetItem) -> Optional[TargetOutcome]:
    """The filed row a fault after the filing commit left behind — the task exists, so the target is filed."""
    row = ctx.session.scalar(select(AutomationRunTarget).where(
        getattr(AutomationRunTarget, ctx.run_column) == ctx.run_id,
        AutomationRunTarget.target_kind == item.kind, AutomationRunTarget.target_id == _key(item),
        AutomationRunTarget.outcome == OUTCOME_FILED,
    ))
    return None if row is None else TargetOutcome(OUTCOME_FILED, row.reason, row.task_id)


def _not_reached(ctx: FireContext, items: list[TargetItem]) -> None:
    for item in items:
        ctx.session.add(_ledger_row(ctx.run_column, ctx.run_id, ctx.automation, item.kind, _key(item), item.name,
                                    OUTCOME_NOT_REACHED, REASON_TIMEOUT))
    ctx.session.commit()


def _fire_schedule(ctx: FireContext, descriptor, resolution, refusal: Optional[str], deadline: float) -> FireResult:
    """A schedule tick or a schedule's Run now: per target, overlap (D10), authority, file."""
    session, automation = ctx.session, ctx.automation
    side = _side_rows(ctx, resolution)
    for item, outcome in side:
        session.add(_ledger_row(ctx.run_column, ctx.run_id, automation, item.kind, _key(item), item.name,
                                outcome.outcome, outcome.reason))
    session.commit()
    results: list[tuple[TargetItem, TargetOutcome]] = list(side)
    targets = list(resolution.fire)
    for index, item in enumerate(targets):
        if time.monotonic() >= deadline:
            _not_reached(ctx, targets[index:])
            results += [(t, TargetOutcome(OUTCOME_NOT_REACHED, REASON_TIMEOUT)) for t in targets[index:]]
            break
        if refusal is not None:
            outcome = TargetOutcome(OUTCOME_ERROR, refusal)
            _record(session, ctx, item, outcome)
        elif previous_still_open(session, automation.id, item.kind, item.id):
            outcome = TargetOutcome(OUTCOME_SKIPPED, REASON_PREVIOUS_OPEN)
            _record(session, ctx, item, outcome)
        else:
            outcome = _file_one(ctx, descriptor, item)
        results.append((item, outcome))
    outcome, detail = _verdict(results, zero=not targets and not side)
    if ctx.cause == CAUSE_MANUAL:
        detail = MANUAL_MARK + detail
    stuck = [o for _, o in results if o.reason == REASON_PREVIOUS_OPEN]
    return FireResult(
        all_previous_open=bool(stuck) and outcome != "acted" and outcome != "error"
        and len(stuck) == len(targets),
        outcome=outcome, detail=detail,
        task_ids=[o.task_id for _, o in results if o.outcome == OUTCOME_FILED and o.task_id is not None],
        counts=_counts(o for _, o in results),
    )


def _counts(outcomes) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
    return counts


def _verdict(results: list[tuple[TargetItem, TargetOutcome]], *, zero: bool) -> tuple[str, str]:
    """The run's outcome by precedence (filed → acted; else error → error; else noop) and its one-line detail."""
    from app.crons import RUN_OUTCOME_ACTED, RUN_OUTCOME_ERROR, RUN_OUTCOME_NOOP

    if zero:
        return RUN_OUTCOME_NOOP, REASON_NO_TARGETS
    filed = [(i, o) for i, o in results if o.outcome == OUTCOME_FILED]
    parts: list[str] = []
    if filed:
        parts.append("filed " + ", ".join(f"#{o.task_id} in {i.name}" for i, o in filed))
    for label, kind in (("errored", OUTCOME_ERROR), ("skipped", OUTCOME_SKIPPED), ("queued", OUTCOME_QUEUED),
                        ("not reached", OUTCOME_NOT_REACHED)):
        rows = [(i, o) for i, o in results if o.outcome == kind]
        if rows:
            parts.append(f"{label} " + ", ".join(f"{i.name} ({o.reason})" if o.reason else i.name for i, o in rows))
    detail = "; ".join(parts)
    if filed:
        return RUN_OUTCOME_ACTED, detail
    if any(o.outcome == OUTCOME_ERROR for _, o in results):
        return RUN_OUTCOME_ERROR, detail
    return RUN_OUTCOME_NOOP, detail


# ── Events ────────────────────────────────────────────────────────────────────────────────────────────


def _owed_keys(delivery: TriggerDelivery) -> Optional[list[str]]:
    """The targets a queued delivery still owes; None for a delivery queued before the engine (it owes its one
    project, read from the rule)."""
    if delivery.owed_targets is None:
        return None
    return [str(entry.get("id") if isinstance(entry, dict) else entry) for entry in delivery.owed_targets]


def owed_targets(session: Session, automation: Automation, delivery: TriggerDelivery) -> list[TargetItem]:
    """The owed targets still resolved by the rule now (a target that left it is no longer owed)."""
    from app import target_resolver

    resolution = target_resolver.resolve(session, automation)
    keys = _owed_keys(delivery)
    if keys is None:
        return list(resolution.fire)
    wanted = set(keys)
    return [item for item in resolution.fire if _key(item) in wanted]


def owed_ready(session: Session, automation: Automation, delivery: TriggerDelivery, cap: int) -> bool:
    """Whether a queued delivery may file now: any owed target below its cap (`trigger_intake._next_row`)."""
    owed = owed_targets(session, automation, delivery)
    # Nothing left to owe (every owed target left the rule) is ready too: the pass closes it.
    return not owed or any(open_in_target(session, automation.id, item.kind, item.id) < cap for item in owed)


def close_owed(session: Session, delivery: TriggerDelivery, reason: str) -> None:
    """A queued delivery the processor ends without filing (its owner lost authority): its owed rows say why.
    Flushes; the caller commits."""
    session.execute(
        update(AutomationRunTarget)
        .where(AutomationRunTarget.delivery_id == delivery.id, AutomationRunTarget.outcome == OUTCOME_QUEUED)
        .values(outcome=OUTCOME_SKIPPED, reason=reason)
    )
    delivery.owed_targets = None


def _fire_event(ctx: FireContext, descriptor, resolution, delivery: TriggerDelivery, refusal: Optional[str],
                deadline: float, bound: float) -> FireResult:
    """An event delivery (or an event's Run now): per target, the concurrency cap (owed, never dropped), authority,
    file. The dedup reservation and rate limit stay per delivery (the processor's); single-target automations go
    through exactly today's states (`queued` / `dispatched` / `skipped: queue full` / `failed`)."""
    from app.triggers import FILING, QUEUED

    session, automation = ctx.session, ctx.automation
    trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.automation_id == automation.id))
    cap = trigger.concurrency_cap if trigger is not None else 1
    depth = trigger.queue_depth if trigger is not None else 0
    existing = {(r.target_kind, r.target_id): r for r in run_targets(session, "delivery_id", delivery.id)}
    state_before = delivery.state
    # Fresh: nothing of this delivery is in the ledger yet — a new delivery, or a Run now's (born `filing`). A
    # resumed crash or a queued delivery is not: its side rows are written and its queue slot already held.
    fresh = state_before != QUEUED and not existing
    if state_before == QUEUED:
        candidates = owed_targets(session, automation, delivery)
        owed_now = set(_owed_keys(delivery) or [_key(i) for i in candidates])
        gone = owed_now - {_key(i) for i in candidates}
    else:
        candidates = [item for item in resolution.fire if (item.kind, _key(item)) not in existing]
        gone = set()
    side = _side_rows(ctx, resolution) if fresh else []
    running = {_key(i): open_in_target(session, automation.id, i.kind, i.id) for i in candidates}
    if refusal is not None:
        below, at_cap = list(candidates), []
    else:
        below = [i for i in candidates if running[_key(i)] < cap]
        at_cap = [i for i in candidates if running[_key(i)] >= cap]
    now = _now()

    if not below and at_cap:
        if state_before == QUEUED:
            session.rollback()
            return FireResult(outcome="noop", detail="", state=QUEUED, reason=delivery.reason)
        room = not fresh or _queued_count(session, delivery) < depth
        for item, outcome in side:
            session.add(_ledger_row("delivery_id", delivery.id, automation, item.kind, _key(item), item.name,
                                    outcome.outcome, outcome.reason))
        if room:
            reason = _waiting_reason(at_cap, running, cap)
            if ctx.cause == CAUSE_MANUAL:
                reason = MANUAL_MARK + reason
            for item in at_cap:
                session.add(_ledger_row("delivery_id", delivery.id, automation, item.kind, _key(item), item.name,
                                        OUTCOME_QUEUED, REASON_WAITING.format(running=running[_key(item)], cap=cap)))
            delivery.state, delivery.reason, delivery.processed_at = QUEUED, reason, now
            delivery.claimed_until = None
            delivery.owed_targets = _owed_entries(at_cap)
            session.commit()
            return FireResult(outcome="noop", detail=reason, state=QUEUED, reason=reason,
                              counts={OUTCOME_QUEUED: len(at_cap)})
        for item in at_cap:
            session.add(_ledger_row("delivery_id", delivery.id, automation, item.kind, _key(item), item.name,
                                    OUTCOME_SKIPPED, REASON_QUEUE_FULL))
        session.flush()
        return _finalize_event(ctx, delivery, owed=[], reason_override=REASON_QUEUE_FULL)

    # Claim: from here on each target commits, so the lease keeps every other pass off this delivery.
    delivery.state = FILING
    delivery.claimed_until = now + timedelta(seconds=max(bound, DEADLINE_SLACK_SECONDS))
    for item, outcome in side:
        session.add(_ledger_row("delivery_id", delivery.id, automation, item.kind, _key(item), item.name,
                                outcome.outcome, outcome.reason))
    skipped_now = {_key(item): reason for item, reason in resolution.skipped}
    for key in gone:
        row = session.scalar(select(AutomationRunTarget).where(
            AutomationRunTarget.delivery_id == delivery.id, AutomationRunTarget.target_id == key,
            AutomationRunTarget.outcome == OUTCOME_QUEUED))
        if row is not None:
            row.outcome, row.reason = OUTCOME_SKIPPED, skipped_now.get(key, REASON_NO_LONGER_TARGETED)
    session.commit()

    owed: list[TargetItem] = []
    missing: list[str] = []
    for index, item in enumerate(below):
        if time.monotonic() >= deadline:
            # Out of time: the rest are owed, never dropped — a later intake pass files them (§4.3).
            owed += below[index:]
            for rest in below[index:]:
                _record(session, ctx, rest, TargetOutcome(OUTCOME_QUEUED, REASON_DEFERRED))
            break
        if refusal is not None:
            _record(session, ctx, item, TargetOutcome(OUTCOME_ERROR, refusal))
            continue
        outcome = _file_one(ctx, descriptor, item)
        missing += [path for path in outcome.missing_paths if path not in missing]
    if at_cap:
        if fresh and _queued_count(session, delivery) >= depth:
            for item in at_cap:
                _record(session, ctx, item, TargetOutcome(OUTCOME_SKIPPED, REASON_QUEUE_FULL))
        else:
            owed += at_cap
            for item in at_cap:
                _record(session, ctx, item, TargetOutcome(
                    OUTCOME_QUEUED, REASON_WAITING.format(running=running[_key(item)], cap=cap)))
    delivery = session.scalar(select(TriggerDelivery).where(TriggerDelivery.id == delivery.id))
    return _finalize_event(ctx, delivery, owed=owed, missing=missing)


def _queued_count(session: Session, delivery: TriggerDelivery) -> int:
    """`queue_depth`'s count: the trigger's deliveries waiting with owed targets (a pre-engine queued row owes its
    one project)."""
    from app.triggers import QUEUED

    return session.scalar(
        select(func.count()).select_from(TriggerDelivery).where(
            TriggerDelivery.trigger_id == delivery.trigger_id, TriggerDelivery.state == QUEUED,
            TriggerDelivery.id != delivery.id,
        )
    ) or 0


def _owed_entries(items: list[TargetItem]) -> list[dict[str, Any]]:
    return [{"kind": item.kind, "id": _key(item), "name": item.name} for item in items]


def _waiting_reason(at_cap: list[TargetItem], running: dict[str, int], cap: int) -> str:
    if len(at_cap) == 1:
        return REASON_WAITING.format(running=running[_key(at_cap[0])], cap=cap)
    return f"waiting: {len(at_cap)} targets each have {cap} filed Tasks still running"


def finalize_delivery(session: Session, delivery: TriggerDelivery, *, error: Optional[str] = None) -> FireResult:
    """Close a `filing` delivery from its ledger alone — the processor's way out when a firing raised past the
    engine (`trigger_intake._file`). `error` is recorded as the delivery's reason when nothing filed."""
    automation = session.scalar(
        select(Automation).join(TaskTrigger, TaskTrigger.automation_id == Automation.id)
        .where(TaskTrigger.id == delivery.trigger_id)
    )
    ctx = FireContext(session=session, automation=automation, params={}, cause=CAUSE_EVENT, owner=None,
                      principal=None, run_column="delivery_id", run_id=delivery.id)
    return _finalize_event(ctx, delivery, owed=[], error=error)


def _finalize_event(ctx: FireContext, delivery: TriggerDelivery, *, owed: list[TargetItem],
                    missing: Optional[list[str]] = None, reason_override: Optional[str] = None,
                    error: Optional[str] = None) -> FireResult:
    """The delivery's verdict from its ledger, its dedup row, and the trigger's health counters — one commit."""
    from app.triggers import DISPATCHED, FAILED, QUEUED, SKIPPED

    session = ctx.session
    rows = run_targets(session, "delivery_id", delivery.id)
    filed = [r for r in rows if r.outcome == OUTCOME_FILED]
    errors = [r for r in rows if r.outcome == OUTCOME_ERROR]
    now = _now()
    trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == delivery.trigger_id).with_for_update())
    first_task = filed[0].task_id if filed else None
    pending = [r for r in rows if r.outcome == OUTCOME_QUEUED]
    if owed or pending:
        owed_entries = _owed_entries(owed) if owed else [
            {"kind": r.target_kind, "id": r.target_id, "name": r.target_name} for r in pending]
        state = QUEUED
        reason = _event_summary(filed, rows, missing) if filed else (
            pending[0].reason if len(pending) == 1 else f"waiting: {len(pending)} targets at their cap")
        delivery.owed_targets = owed_entries
    elif filed:
        state, reason = DISPATCHED, _event_summary(filed, rows, missing)
        delivery.owed_targets = None
    elif errors or error:
        state = FAILED
        reason = errors[0].reason if errors else error
        if not reason.startswith("filing failed:"):
            reason = REASON_FILING_FAILED.format(error=reason)
        delivery.owed_targets = None
    else:
        state = SKIPPED
        reason = reason_override or (REASON_NO_TARGETS if not rows else _skip_summary(rows))
        delivery.owed_targets = None
    if ctx.cause == CAUSE_MANUAL and not reason.startswith(MANUAL_MARK):
        reason = MANUAL_MARK + reason
    delivery.state = state
    delivery.reason = reason
    delivery.processed_at = now
    delivery.claimed_until = None
    delivery.task_id = first_task
    if missing:
        delivery.missing_paths = list(missing)
    authority = [r.reason for r in rows if r.reason.startswith(_AUTHORITY_PREFIXES)]
    _close_delivery(session, delivery, trigger, state, reason, acted=bool(filed), first_task=first_task,
                    authority_reason=authority[0] if state == SKIPPED and rows and len(authority) == len(rows)
                    else None)
    session.commit()
    task_ids = [r.task_id for r in filed if r.task_id is not None]
    outcome = "acted" if filed else ("error" if state == FAILED else "noop")
    return FireResult(outcome=outcome, detail=reason, task_ids=task_ids, counts=_counts(rows),
                      state=state, reason=reason)


def _close_delivery(session: Session, delivery: TriggerDelivery, trigger: Optional[TaskTrigger], state: str,
                    reason: str, *, acted: bool, first_task: Optional[int], authority_reason: Optional[str]) -> None:
    """A delivery's dedup reservation (held by what it filed, released when nothing did) and its trigger's health
    counters; `authority_reason` marks the trigger failed outright (every target refused on the owner's authority).
    Flushes nothing itself; the caller commits."""
    from app import trigger_health
    from app.triggers import DISPATCHED, FAILED, QUEUED, SKIPPED

    now = _now()
    if delivery.dedup_key:
        if acted:
            session.execute(
                update(TriggerDedup)
                .where(TriggerDedup.trigger_id == delivery.trigger_id, TriggerDedup.dedup_key == delivery.dedup_key)
                .values(task_id=first_task, delivery_id=delivery.id)
            )
        elif state in (FAILED, SKIPPED):
            session.execute(delete(TriggerDedup).where(
                TriggerDedup.trigger_id == delivery.trigger_id, TriggerDedup.dedup_key == delivery.dedup_key,
                TriggerDedup.delivery_id == delivery.id,
            ))
    if state in (FAILED, SKIPPED):
        delivery.event = None
    if trigger is None:
        return
    if state == DISPATCHED or (state == QUEUED and acted):
        trigger_health.apply_outcome(trigger, DISPATCHED, now=now)
    elif state in (FAILED, SKIPPED):
        if delivery.api_key_id is not None and (authority_reason is not None or REASON_KEY_GONE in reason):
            # The key's owner's authority, or a key that stopped working: that delivery's fault, never the trigger's
            # (any member's key would otherwise stop it for everyone).
            return
        if authority_reason is not None:
            trigger_health.mark_failed(session, trigger, authority_reason, now=now)
        else:
            trigger_health.apply_outcome(trigger, state, reason=reason, now=now)
            trigger_health.auto_pause_if_due(session, trigger, now=now)


def _event_summary(filed: list[AutomationRunTarget], rows: list[AutomationRunTarget],
                   missing: Optional[list[str]] = None) -> str:
    """A delivery's reason once it filed: a single target reads exactly as a trigger's always has."""
    empty = f" — empty: {', '.join(missing)}" if missing else ""
    if len(rows) == 1 and len(filed) == 1:
        return f"Filed Task #{filed[0].task_id}{empty}"
    head = "Filed " + ", ".join(f"Task #{r.task_id} in {r.target_name}" for r in filed) + empty
    rest = [r for r in rows if r.outcome not in (OUTCOME_FILED,)]
    if rest:
        head += "; " + ", ".join(f"{r.target_name}: {r.outcome}" + (f" ({r.reason})" if r.reason else "")
                                 for r in rest)
    return head


def _skip_summary(rows: list[AutomationRunTarget]) -> str:
    if len(rows) == 1:
        return rows[0].reason or rows[0].outcome
    return "nothing filed: " + ", ".join(f"{r.target_name} ({r.reason or r.outcome})" for r in rows)


# ── Schedule bookkeeping ─────────────────────────────────────────────────────────────────────────────


def close_interrupted_runs(session: Session, cron_id: UUID, *, keep: Optional[UUID] = None) -> int:
    """R-8: a schedule run's provisional row whose firing died (the process went down mid-fan-out) is closed at the
    next tick — the caller holds the cron's run lock, so no live firing owns it. With no target written it is an
    `error` "Interrupted"; otherwise its outcome follows its targets by precedence. Flushes; returns how many."""
    from app.crons import RUN_OUTCOME_ACTED

    statement = select(CronRun).where(
        CronRun.cron_id == cron_id, CronRun.outcome == RUN_OUTCOME_ACTED,
        CronRun.started_at.is_not(None), CronRun.finished_at.is_(None),
    )
    if keep is not None:
        statement = statement.where(CronRun.id != keep)
    rows = session.scalars(statement).all()
    for run in rows:
        verdict = ledger_verdict(session, run.id)
        if verdict is None:
            run.outcome, run.detail = "error", "Interrupted"
        else:
            outcome, detail, run.task_id = verdict
            run.outcome, run.detail = outcome, f"Interrupted: {detail}"
        run.finished_at = _now()
    session.flush()
    return len(rows)


def ledger_verdict(session: Session, cron_run_id: UUID) -> Optional[tuple[str, str, Optional[int]]]:
    """(outcome, detail, first filed task) of a schedule run from its ledger alone, or None when it wrote no
    target — how a run is settled when its firing died part-way."""
    targets = run_targets(session, "cron_run_id", cron_run_id)
    if not targets:
        return None
    pairs = [(TargetItem(kind=t.target_kind, id=t.target_id, name=t.target_name),
              TargetOutcome(t.outcome, t.reason, t.task_id)) for t in targets]
    outcome, detail = _verdict(pairs, zero=False)
    return outcome, detail, next((t.task_id for t in targets if t.outcome == OUTCOME_FILED), None)


def has_filed(session: Session, delivery_id: int) -> bool:
    """Did any target of this delivery file? Its dedup reservation stands exactly when one did."""
    return session.scalar(select(AutomationRunTarget.id).where(
        AutomationRunTarget.delivery_id == delivery_id, AutomationRunTarget.outcome == OUTCOME_FILED).limit(1)
    ) is not None


def fire_capable(session: Session, automation_id: Optional[UUID]) -> Optional[Automation]:
    """The automation a child runs through the engine, or None: a tenant automation whose action has a filer (a Task,
    or Send email with no Target).
    System functions and noop keep their activity's own `invoke` (PRD §5)."""
    from app import automation_actions

    if automation_id is None:
        return None
    automation = session.scalar(select(Automation).where(Automation.id == automation_id))
    if automation is None or automation.is_system:
        return None
    descriptor = automation_actions.lookup(automation.action_type)
    if descriptor is None or descriptor.fire is None:
        return None
    return automation
