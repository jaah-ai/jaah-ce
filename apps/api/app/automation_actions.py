"""The automation action registry — what an automation can DO, and the descriptor that says so (AC4.5).

An automation names an `action_type`; this is the single home for what those names mean. The API lists the types a
caller may create (`creatable_for`), validates and stores their params (`ActionDescriptor.validate`), and the fire
engine runs them (`ActionDescriptor.fire`; `task`'s is `automation_fire.file_task_target`). Adding an action type is one
`register(...)` here plus its web form — no change to the editor shell, the list or the service.

Four kinds are registered:
* `task` — files one Task per resolved target (projects or nodes), reusing the task-create validator
  (`routes/tasks/create.py::validate_task_spec`) so a field added to task create validates here too;
* `email.send` — sends one message from the org's connected Gmail per firing, no target (`app/automation_email.py`);
* `noop` — the cron pipeline's side-effect-free probe (`cron_activities.NOOP_ACTIVITY`), no target;
* every SYSTEM cron activity (`cron_activities`, `is_system=True`) wrapped as an operator-only System function with
  no target; `creatable` is the activity's own, so a deleted one comes back only where it may.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features
from app.automations import ACTION_EMAIL, ACTION_NOOP, ACTION_TASK, MODE_SPECIFIC, TARGET_NODES, TARGET_PROJECTS, WHEN_EVENT
from app.entity_shape import optional_uuid

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.authz.principal import Principal
    from app.cron_activities import ActivityDescriptor
    from app.models.identity import AdminUser


class ActionRegistryError(ValueError):
    """A descriptor (or a registration) that cannot be honoured; raised at registration time."""


@dataclass(frozen=True)
class TargetRule:
    """The wire `TargetRule` (§6.1): a live rule, never an expanded id list. `ids` are project ints or node uuid
    strings; `[]` for `all`; `specific` with `[]` is the editor's "None selected"."""

    kind: str
    mode: str
    ids: tuple[Any, ...] = ()


@dataclass(frozen=True)
class AutomationDraft:
    """What a validator may know about the automation being saved. `automation_id` is None on create."""

    org_id: UUID
    when_kind: str
    target: Optional[TargetRule]
    actor: "AdminUser"
    principal: "Principal"
    automation_id: Optional[UUID] = None


@dataclass(frozen=True)
class ActionDescriptor:
    """One thing an automation can be pointed at (§5.1).

    `operator_only` gates who may create, edit or see it (every system activity); `takes_target` says whether the
    automation carries a Target at all, and `target_kinds` which. `validate(session, draft, params)` returns the
    normalised params or raises a 422. `fire` is the fire engine's per-target filer (None → the legacy activity
    `invoke` runs, as for system functions and `noop`); `system_activity` is the wrapped cron activity, if any.
    """

    type: str
    label: str
    operator_only: bool
    takes_target: bool
    target_kinds: tuple[str, ...]
    timeout_seconds: int
    validate: Callable[[Session, AutomationDraft, dict[str, Any]], dict[str, Any]]
    fire: Optional[Callable[..., Any]] = None
    system_activity: Optional["ActivityDescriptor"] = None
    creatable: bool = True
    description: str = ""
    # Offered in the picker to operators only; unlike `operator_only`, a tenant may still keep, edit and run one.
    operator_listed: bool = False
    # Whether an event (a trigger) may fire it; every action runs on a schedule.
    event_capable: bool = False

    def __post_init__(self) -> None:
        if not self.type or not self.type.strip():
            raise ActionRegistryError("an action `type` must be a non-empty identifier")
        if not self.label.strip():
            raise ActionRegistryError(f"action {self.type!r} must carry a label")
        if self.takes_target and not self.target_kinds:
            raise ActionRegistryError(f"action {self.type!r} takes a target but names no target kind")
        if not self.takes_target and self.target_kinds:
            raise ActionRegistryError(f"action {self.type!r} takes no target, so it may name no target kind")
        unknown = set(self.target_kinds) - {TARGET_PROJECTS, TARGET_NODES}
        if unknown:
            raise ActionRegistryError(f"action {self.type!r} names unknown target kinds {sorted(unknown)}")
        if self.timeout_seconds <= 0:
            raise ActionRegistryError(f"action {self.type!r}: timeout_seconds must be positive")
        if not callable(self.validate):
            raise ActionRegistryError(f"action {self.type!r}: `validate` must be callable")


_REGISTRY: dict[str, ActionDescriptor] = {}
_LOADED = False
#: The types only one feature carries, `{type: feature}`: an edition without it neither offers nor accepts the type.
#: Send email needs the org's connected Gmail.
ACTION_FEATURES: dict[str, str] = {ACTION_EMAIL: "tenant_mail"}


def _in_edition(action_type: str) -> bool:
    feature = ACTION_FEATURES.get(action_type)
    return feature is None or edition_features.enabled(feature)


def register(descriptor: ActionDescriptor) -> ActionDescriptor:
    """Add one descriptor. Refuses a duplicate `type`: otherwise the last import wins silently."""
    if descriptor.type in _REGISTRY:
        raise ActionRegistryError(f"action {descriptor.type!r} is already registered")
    _REGISTRY[descriptor.type] = descriptor
    return descriptor


def unregister(action_type: str) -> None:
    """Remove one descriptor — for a test's dummy type only."""
    _REGISTRY.pop(action_type, None)


def _load() -> None:
    """Register the core types and wrap every system cron activity. Idempotent."""
    global _LOADED
    if _LOADED:
        return
    from app import cron_activities

    _LOADED = True
    for activity in cron_activities.all_descriptors():
        if not activity.is_system or activity.type in _REGISTRY:
            continue
        register(ActionDescriptor(
            type=activity.type,
            label=activity.label,
            description=activity.description,
            operator_only=True,
            takes_target=False,
            target_kinds=(),
            timeout_seconds=int(activity.timeout_seconds or _SYSTEM_TIMEOUT_SECONDS),
            validate=_activity_params_validator(activity),
            system_activity=activity,
            creatable=activity.creatable,
        ))


def get(action_type: str) -> ActionDescriptor:
    """The descriptor for `action_type`, or a 422 naming the known ones; a type this edition lacks is its 404."""
    _load()
    if action_type in _REGISTRY and not _in_edition(action_type):
        from app.ce_caps import NOT_IN_CE

        raise HTTPException(status_code=404, detail=NOT_IN_CE)
    descriptor = _REGISTRY.get(action_type)
    if descriptor is None:
        raise HTTPException(
            status_code=422,
            detail=f"action.type: no action {action_type!r}; known: {', '.join(sorted(filter(_in_edition, _REGISTRY)))}",
        )
    return descriptor


def lookup(action_type: str) -> Optional[ActionDescriptor]:
    """The descriptor, or None for a type no longer registered (a stored row naming a retired activity)."""
    _load()
    return _REGISTRY.get(action_type)


def all_descriptors() -> tuple[ActionDescriptor, ...]:
    _load()
    return tuple(sorted(_REGISTRY.values(), key=lambda d: (d.operator_only, d.label.lower(), d.type)))


def may_use(descriptor: ActionDescriptor, principal: "Principal") -> bool:
    """Whether this caller may create, edit or see automations of this type: an operator-only (system) type is
    the operator's alone (AC4.4, AC4.7)."""
    return not descriptor.operator_only or principal.is_operator


def creatable_for(principal: "Principal") -> list[ActionDescriptor]:
    """The types this caller's picker offers."""
    return [
        d for d in all_descriptors()
        if d.creatable and may_use(d, principal) and (not d.operator_listed or principal.is_operator)
        and _in_edition(d.type)
    ]


# ── System functions and noop: the wrapped activity's own params schema ───────────────────────────────

# `cron_runtime`'s default wall clock when an activity declares none.
_SYSTEM_TIMEOUT_SECONDS = 60


def _activity_params_validator(activity: "ActivityDescriptor") -> Callable[..., dict[str, Any]]:
    def validate(_session: Session, _draft: AutomationDraft, params: dict[str, Any]) -> dict[str, Any]:
        from app import cron_activities

        try:
            return cron_activities.validate_params(activity, params)
        except cron_activities.ParamsError as exc:
            raise HTTPException(status_code=422, detail=f"action.params: {exc}") from exc

    return validate


def _noop_validate(session: Session, draft: AutomationDraft, params: dict[str, Any]) -> dict[str, Any]:
    from app.cron_activities import NOOP_ACTIVITY

    return _activity_params_validator(NOOP_ACTIVITY)(session, draft, params)


# ── The Task action (§5.2) ────────────────────────────────────────────────────────────────────────────

#: Exactly the keys a Task action's params carry, with the value an absent key reads as.
TASK_PARAM_DEFAULTS: dict[str, Any] = {
    "persona": "coder",
    "body": "",
    # A template the task is filed from instead of `body` (exactly one of the two): resolved live at each fire.
    "template_id": None,
    "title_template": None,
    "model": None,
    "bound_provider": None,
    "autonomous": True,
    "project_memory": None,
    "priority": None,
    "playbook_id": None,
    "transition_mode": None,
    "assignee_id": None,
    "attachment_ids": [],
    "feature_id": None,
    "hidden": False,
    # False files the task as a `draft` an operator starts by hand; it is never dispatched until then.
    "start": True,
}

# Task-create fields an automation deliberately does not offer (O3, R19): each is a 422 naming why.
_REFUSED_TASK_FIELDS: dict[str, str] = {
    "dependencies": "Dependencies are not offered on an automation's Task action",
    "depends_on": "Dependencies are not offered on an automation's Task action",
    "external_task_system": "a Jira link is not offered on an automation's Task action",
    "external_task_id": "a Jira link is not offered on an automation's Task action",
}

# The longest body a Task action stores (the Runner's instructions cap; triggers' template cap is the same).
MAX_TASK_BODY_CHARS = 20_000
MAX_TITLE_TEMPLATE_CHARS = 500


def _refuse(field_name: str, message: str) -> None:
    raise HTTPException(status_code=422, detail=f"action.params.{field_name}: {message}")


def task_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    """A stored Task action's params with every key present (absent keys read as their defaults)."""
    merged = dict(TASK_PARAM_DEFAULTS)
    merged.update({k: v for k, v in (params or {}).items() if k in TASK_PARAM_DEFAULTS})
    merged["attachment_ids"] = list(merged.get("attachment_ids") or [])
    return merged


def _task_validate(session: Session, draft: AutomationDraft, params: dict[str, Any]) -> dict[str, Any]:
    """Normalise and check a Task action's params; the field checks are task create's own."""
    from app.routes.tasks.policy import PERSONA_EPIC
    from app.routes.tasks.create import TaskCreateSpec, _provider_on_plan_or_422, validate_task_spec

    if not isinstance(params, dict):
        _refuse("", "must be an object")
    for key, why in _REFUSED_TASK_FIELDS.items():
        if key in params:
            _refuse(key, why)
    unknown = sorted(set(params) - set(TASK_PARAM_DEFAULTS))
    if unknown:
        _refuse(unknown[0], f"is not a Task action field; expected one of {', '.join(TASK_PARAM_DEFAULTS)}")
    out = task_params(params)

    body = out["body"]
    if not isinstance(body, str):
        _refuse("body", "is required")
    template = _template_or_refuse(session, draft, out["template_id"], has_body=bool(body.strip()))
    if template is None and not body.strip():
        _refuse("body", "is required, or a template_id")
    if len(body) > MAX_TASK_BODY_CHARS:
        _refuse("body", f"is {len(body)} characters; the limit is {MAX_TASK_BODY_CHARS}")
    out["body"] = body
    out["template_id"] = str(template.id) if template is not None else None
    for flag in ("autonomous", "hidden", "start"):
        if not isinstance(out[flag], bool):
            _refuse(flag, "must be true or false")
    if out["project_memory"] is not None and not isinstance(out["project_memory"], bool):
        _refuse("project_memory", "must be true, false or null")

    title = out["title_template"]
    if title is not None:
        if not isinstance(title, str):
            _refuse("title_template", "must be text")
        if draft.when_kind != WHEN_EVENT:
            _refuse("title_template", "only an event automation renders a title; a schedule titles from the body")
        if len(title) > MAX_TITLE_TEMPLATE_CHARS:
            _refuse("title_template", f"is {len(title)} characters; the limit is {MAX_TITLE_TEMPLATE_CHARS}")
        out["title_template"] = title if title.strip() else None

    # A template supplies the work fields at each fire, so they are checked as its, and the action's own are unused.
    persona = template.persona if template is not None else out["persona"]
    if out["transition_mode"] is not None and persona != PERSONA_EPIC:
        _refuse("transition_mode", "applies to an epic only")

    # The task-create validator, so the persona set, the model and provider pins and their authorization are the
    # create route's own. Only keys actually sent are "provided", as on the create route. A template's model pin is
    # its editor's choice, never re-authorized against the automation's owner (as on Start).
    spec_fields: dict[str, Any] = {
        "body": template.body if template is not None else body, "persona": persona,
        "autonomous": template.autonomous if template is not None else out["autonomous"],
    }
    for name in ("priority", "project_memory", "transition_mode", "playbook_id"):
        if out[name] is not None:
            spec_fields[name] = out[name]
    for name in ("bound_provider",) if template is not None else ("model", "bound_provider"):
        if params.get(name) not in (None, ""):
            spec_fields[name] = params[name]
    try:
        spec = TaskCreateSpec(**spec_fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"action.params: {exc}") from exc
    pins = validate_task_spec(spec, draft.principal)
    out["model"], out["bound_provider"] = pins.model, pins.provider
    # A pin with no cell on the org's provider grid is refused here, as on task create; firing re-checks it.
    _provider_on_plan_or_422(session, pins.provider, draft.org_id)
    out["priority"] = spec.priority
    if spec.playbook_id is not None:
        from app import playbook_engine

        try:
            playbook_engine.load(session, draft.org_id, spec.playbook_id)
        except playbook_engine.PlaybookNotRunnable as exc:
            raise HTTPException(status_code=422, detail=f"action.params.playbook_id: {exc}") from exc
        out["playbook_id"] = str(spec.playbook_id)

    out["assignee_id"] = _assignee_or_refuse(session, draft, out["assignee_id"])
    out["attachment_ids"] = _attachments_or_refuse(session, draft, out["attachment_ids"])
    out["feature_id"] = _feature_or_refuse(session, draft, out["feature_id"])
    return out


def _template_or_refuse(session: Session, draft: AutomationDraft, value: Any, *, has_body: bool) -> Optional[Any]:
    """The template a Task action files from: visible to the automation's org and published (`task_templates.startable`,
    Start's own rule). Exactly one of it and a body."""
    from app import task_templates

    if value in (None, ""):
        return None
    if has_body:
        _refuse("template_id", "send a body or a template_id, not both")
    template_id = optional_uuid(value)
    template = task_templates.startable(session, template_id, draft.org_id) if template_id is not None else None
    if template is None:
        _refuse("template_id", "Template unavailable")
    return template


def _assignee_or_refuse(session: Session, draft: AutomationDraft, value: Any) -> Optional[str]:
    """Optional (O3). At save it must be a real, non-system user of the automation's org; whether they reach each
    target is asked per target at fire time (an unreachable target files unassigned)."""
    from app.models.identity import AdminUser

    if value in (None, ""):
        return None
    user_id = optional_uuid(value)
    if user_id is None:
        _refuse("assignee_id", "must be a user id")
    user = session.scalar(select(AdminUser).where(AdminUser.id == user_id, AdminUser.org_id == draft.org_id))
    if user is None or user.is_system:
        _refuse("assignee_id", "must name a real user of this automation's organization")
    return str(user_id)


def _attachments_or_refuse(session: Session, draft: AutomationDraft, value: Any) -> list[int]:
    """Files already uploaded onto THIS automation (`POST /api/automations/{id}/attachments`); a new automation
    has none yet — the editor uploads its staged files right after the create."""
    from app.models.task_extras import TaskAttachment

    if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) for v in value):
        _refuse("attachment_ids", "must be a list of attachment ids")
    ids = sorted(set(value))
    if not ids:
        return []
    if draft.automation_id is None:
        _refuse("attachment_ids", "upload files after the automation is created, then save their ids")
    found = set(session.scalars(
        select(TaskAttachment.id).where(TaskAttachment.id.in_(ids), TaskAttachment.automation_id == draft.automation_id)
    ).all())
    missing = [i for i in ids if i not in found]
    if missing:
        _refuse("attachment_ids", f"attachment #{missing[0]} is not a file of this automation")
    return ids


def _feature_or_refuse(session: Session, draft: AutomationDraft, value: Any) -> Optional[str]:
    """A feature is offered only when the target is exactly one specific project, and must be an open feature of
    it (the create route's rule: a shipped feature is closed for filing)."""
    from app.features import FEATURE_SHIPPED
    from app.models.projects import Feature

    if value in (None, ""):
        return None
    target = draft.target
    if target is None or target.kind != TARGET_PROJECTS or target.mode != MODE_SPECIFIC or len(target.ids) != 1:
        _refuse("feature_id", "a feature can be set only when the target is exactly one specific project")
    feature_id = optional_uuid(value)
    if feature_id is None:
        _refuse("feature_id", "must be a feature id")
    feature = session.scalar(select(Feature).where(Feature.id == feature_id))
    if feature is None or feature.project_id != target.ids[0]:
        _refuse("feature_id", f"feature {feature_id} is not a feature of the target project")
    if feature.status == FEATURE_SHIPPED:
        _refuse("feature_id", "this feature is shipped, so it is closed for filing")
    return str(feature_id)


def _task_fire(ctx: Any, target: Any) -> Any:
    """The Task action's per-target filer — the fire engine's (`automation_fire.file_task_target`), imported on
    use because it reaches the route layer."""
    from app.automation_fire import file_task_target

    return file_task_target(ctx, target)


TASK_ACTION = register(ActionDescriptor(
    type=ACTION_TASK,
    label="Task",
    description="Files one task per target each time it runs.",
    operator_only=False,
    takes_target=True,
    target_kinds=(TARGET_PROJECTS, TARGET_NODES),
    # No fan-out cap (O2): a large firing ends in `not_reached` rows at its deadline, never a hang.
    timeout_seconds=600,
    validate=_task_validate,
    fire=_task_fire,
    event_capable=True,
))


def _email_validate(session: Session, draft: AutomationDraft, params: dict[str, Any]) -> dict[str, Any]:
    from app import automation_email

    return automation_email.validate(session, draft, params)


def _email_fire(ctx: Any, target: Any) -> Any:
    from app import automation_email

    return automation_email.fire(ctx, target)


EMAIL_ACTION = register(ActionDescriptor(
    type=ACTION_EMAIL,
    label="Send email",
    description="Sends an email from the org's connected Gmail each time it runs.",
    operator_only=False,
    takes_target=False,
    target_kinds=(),
    timeout_seconds=120,
    validate=_email_validate,
    fire=_email_fire,
    event_capable=True,
))

NOOP_ACTION = register(ActionDescriptor(
    type=ACTION_NOOP,
    label="No-op probe",
    description="Records a run and does nothing else.",
    operator_only=False,
    # A diagnostic: a tenant's picker never offers it.
    operator_listed=True,
    takes_target=False,
    target_kinds=(),
    timeout_seconds=30,
    validate=_noop_validate,
))
