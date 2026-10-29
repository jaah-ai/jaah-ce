"""Task creation: the create route, its spec validation, playbook plans, epic children and body versions."""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import actors, agent_providers, model_catalog, node_targets, provider_auth_policy
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.object_access import can_access, named_user_reaches_project
from app.authz.principal import Principal, current_principal
from app.authz.visibility import PROJECTS_VIEW_KEY, ObjectAccess, has_project_key, readable_account_select
from app.db import get_session
from app.entity_shape import SYSTEM_ACTOR_ID
from app.events import emit_console_event
from app.features import FEATURE_SHIPPED
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.projects import Feature, Project
from app.models.tasks import Task, TaskBodyVersion, TaskDependency
from app.persona_registry import CREATABLE_PERSONAS
from app import agent_resolution, placement_grants, playbook_engine, task_templates
from app.models.task_templates import OrgTaskTemplate
from app.task_template_assets import copy_template_assets
from app.task_lifecycle import DRAFT, READY
from app.task_lookup import get_task_or_404
from app import task_source
from app.task_title import UNSAVED_DRAFT_TITLE, derived_child_title, placeholder_title
from app.tenancy.copy import administrator_changes
from app.tenancy.ids import is_tenant_org
from app.tenancy.labels import org_refs
from app.routes.tasks.policy import (
    DEFAULT_TRANSITION_MODE,
    INTERVIEWING_CHILD_PERSONAS,
    PERSONA_ADVISOR,
    PERSONA_ARCHITECTURE,
    PERSONA_BUSINESS_ANALYSIS,
    PERSONA_CODER,
    PERSONA_EPIC,
    PERSONA_PLAN,
    MAX_TASK_TITLE_LEN,
    TRANSITION_MODES,
    _DISPATCH_COMPOSED_BODY,
    _PRIORITY_ABS_MAX,
    _advisor_chat_cap_or_409,
    create_autonomous_for_persona,
)
from app.routes.tasks.task_wire import TaskOut, _authored_draft_ids, _may_edit_ids, _task_out
from app.routes.tasks.dependencies import _would_create_cycle

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# The trackers `external_task_system` may name; wire strings, every write-path check builds from it.
EXTERNAL_TASK_SYSTEMS = ("jira",)
# SECURITY boundary: the issue key is later interpolated into a Jira REST path AND a JQL string (JQL has
# no parameterized form), so the strict check must sit at the WRITE boundary.
_EXTERNAL_TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$")


def _external_issue_or_422(
    system: Optional[str], issue_id: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Normalise + validate the external-issue PAIR, returning the `(system, id)` to STORE. Blank collapses
    to null (an emptied field CLEARS the link); each half is checked; both-set or both-null, never exactly
    one (models/tasks.py:Task.external_task_system). Every failure is a 422."""
    system = system.strip() or None if system is not None else None
    issue_id = issue_id.strip() or None if issue_id is not None else None
    if system is not None and system not in EXTERNAL_TASK_SYSTEMS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"external_task_system must be one of {', '.join(EXTERNAL_TASK_SYSTEMS)} "
                f"(got {system!r})"
            ),
        )
    if issue_id is not None and not _EXTERNAL_TASK_ID_RE.match(issue_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"external_task_id must be a tracker issue key like 'OAM-123' "
                f"(got {issue_id!r})"
            ),
        )
    if (system is None) != (issue_id is None):
        raise HTTPException(
            status_code=422,
            detail=(
                "external_task_system and external_task_id must be set together or both left empty "
                f"(got system={system!r}, id={issue_id!r})"
            ),
        )
    return system, issue_id


def _model_pin_or_refuse(
    value: Optional[str], principal: Principal | None
) -> Optional[str]:
    """The model pin, authorized then validated; shared by `create_task` and `update_task`.

    Called on PRESENCE of the key, not on a change. Returns the normalized value, INHERIT collapsed to
    `None`. AUTHORIZATION BEFORE VALIDATION: a refused principal learns nothing about accepted values.
    The bar is asked directly, never via a feature key (no grant confers it): in a tenant org the org-admin
    rule (`Principal.is_org_admin`), on the operator plane the superadmin flag. `principal is None` is
    the agent/infrastructure principal, allow-all as everywhere in this module."""
    if not (principal is None or (
        principal.is_org_admin if is_tenant_org(principal.org_id) else principal.is_superadmin
    )):
        # The sentence differs by seat: a tenant seat reads who can act in their console's words
        # (`app/tenancy/copy.py` never serves "superadmin" there).
        if is_tenant_org(principal.org_id):
            raise HTTPException(status_code=403, detail=administrator_changes("Task models"))
        raise HTTPException(status_code=403, detail="only a superadmin may set a task's model")
    try:
        return model_catalog.validate_model(value) or None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"model: {exc}") from exc


def _provider_pin_or_refuse(value: Optional[str]) -> Optional[str]:
    """The operator's provider pin, validated; shared by `create_task` and `update_task`.

    It writes `tasks.bound_provider`, the binding `routes/fleet.py::_provider_narrowed` already enforces, so
    "only run on this vendor" needs no second dispatch rule. Empty/None is "Any" and stores NULL: an unbound
    row, which the first claim stamps (`_bound_provider_stamp`). It is the built-in half of the one agent
    picker, so it takes the route's own key (`tasks:create` / `tasks:edit`, DD13), never an admin tier; the
    closed set is `app/agent_providers.py::PROVIDER_VALUES`."""
    if not value:
        return None
    if value not in agent_providers.PROVIDER_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"provider must be one of {', '.join(agent_providers.PROVIDER_VALUES)} (got {value!r})",
        )
    return value


#: The one picker writes one half (DD12, `ck_tasks_agent_or_provider`).
_AGENT_OR_PROVIDER = "Pick a built-in runner or an SDK agent for this task, not both"


def _provider_on_plan_or_422(session: Session, provider: Optional[str], org_id: UUID) -> None:
    """422 when `provider` (a built-in pin) has no cell on the org's provider grid
    (`provider_auth_policy.provider_on_plan`); dispatch would refuse its every account anyway."""
    if provider and not provider_auth_policy.provider_on_plan(session, org_id, provider):
        raise HTTPException(status_code=422, detail=provider_auth_policy.refusal(session, org_id, provider))


def _agent_pick_or_422(
    session: Session, agent_id: Optional[UUID], org_id: UUID, *, current: Optional[UUID] = None
) -> Optional[UUID]:
    """The SDK half of the picker (`tasks.agent_id`), refused unless it names an agent the task's org may use:
    its own, or a catalog connector it enabled. One 422 for "no such agent" and "another org's", so the field
    is no cross-org existence oracle (N7). A disabled, unfetched or persona-mismatched agent is accepted here:
    dispatch names why it cannot run (`agent_resolution`, DD19). None clears the pick. A new pick (not
    `current`, which an edit form resends) is refused while the agent's provider-grid cell is off for the org
    (`agent_resolution.cell_refusal`)."""
    if agent_id is None:
        return None
    agent = agent_resolution.load_agent(session, agent_id)
    if agent is None or not agent_resolution.visible_to(session, agent, org_id):
        raise HTTPException(status_code=422, detail="agent_id names no agent this organization can use")
    off = agent_resolution.cell_refusal(session, agent, org_id) if agent_id != current else None
    if off is not None:
        raise HTTPException(status_code=422, detail=off)
    return agent_id


_NO_SUCH_ACCOUNT = "account_id names no account you can use in this organization"


def _account_pick_or_422(
    session: Session, account_id: Optional[UUID], principal: Principal | None, org_id: UUID,
    provider: Optional[str],
) -> Optional[Account]:
    """The account pin (`tasks.bound_account_id`): an account of the task's org the caller may see on the roster
    (`readable_account_select`, the `GET /api/accounts` rule). One 422 for "no such account", "another org's" and
    "not yours", so the field is no existence oracle. A `provider` pin must be the account's own vendor."""
    if account_id is None:
        return None
    account = session.scalar(
        readable_account_select(principal).where(Account.id == account_id, Account.org_id == org_id)
    )
    if account is None:
        raise HTTPException(status_code=422, detail=_NO_SUCH_ACCOUNT)
    if provider and provider != account.provider:
        raise HTTPException(
            status_code=422,
            detail=f"account {account.slug} runs {account.provider}, not {provider}: pick one or the other",
        )
    return account


# `_apply_project_move`'s "no pin in this save": the moved task's stored pin is the one checked.
_KEEP_PIN = object()
_NODE_PIN_OFF = (
    "this org has not turned on picking a task's node (Settings > Tasks > Node picker); leave Node out of the request"
)


def _may_pin_node_or_403(principal: Principal | None, org_id: Optional[UUID]) -> None:
    """Who may send a node pin: an operator always, anyone else (already a writer of the task) only while the
    task's org has `task_node_pin_enabled` on (`placement_grants.node_pin_enabled`). `principal is None` is
    allow-all, as everywhere in this module."""
    if principal is None or principal.is_operator or placement_grants.node_pin_enabled(org_id):
        return
    raise HTTPException(status_code=403, detail=_NODE_PIN_OFF)


def _pinned_node_or_refuse(
    session: Session, node_id: Optional[UUID], principal: Principal | None, project: Project
) -> Optional[UUID]:
    """A node pin (`models.Task.pinned_node_id`), authorized then validated; shared by `create_task` and
    `update_task`. Called on PRESENCE of the key, gated by `_may_pin_node_or_403`, a hidden field being no
    access control. None clears the pin. A value must be a node the project runs on
    (`placement_grants.project_node_ids`, the set placement narrows a pin within): one 422 for "no such node",
    "another org's" and "not this project's"."""
    _may_pin_node_or_403(principal, project.org_id)
    if project.system_kind is not None:
        # A node task's pin is its automation's (`app/node_targets.py`), neither moved nor cleared here.
        raise HTTPException(status_code=422, detail="a node task runs on the node its automation picked")
    if node_id is None:
        return None
    if node_id not in placement_grants.project_node_ids(session, project.id, project.org_id):
        raise HTTPException(
            status_code=422,
            detail=(
                "pinned_node_id names no node this project runs on — assign the node to the project "
                "first, or pick Any"
            ),
        )
    return node_id


def _validated_assignee(
    session: Session, value: UUID, *, project_id: int, actor: AdminUser
) -> UUID:
    """`assigned_to_id`, refused unless it names a real, non-system user who can ALREADY reach the task's
    PROJECT; shared by `create_task` and `update_task` so the two doors onto one column cannot drift.

    The assignee is the addressee (`task_notify.announce_assignment` DMs them the title), and any
    `tasks:edit` holder can edit the task, so unchecked it would route content to an arbitrary account.
    The candidate set is exactly what `list_project_members` offers: a direct or group grant on the
    project, or an operator-plane superadmin.

    The reach probe lives once in `authz.named_user_reaches_project` (including the tenant-actor
    superadmin flag-drop); the 422 sentence stays with each door, as in
    `routes/projects.py::_validated_project_role`.

    The system actor is refused ahead of the probe (non-loginable, but superadmin-shaped). 422 naming
    the field, never the account: echoing the email would be a uuid→address oracle. By id first: its row
    sits in org zero (revision 0406), which a tenant caller's scope does not read."""
    user = session.get(AdminUser, value)
    if user is None and value != SYSTEM_ACTOR_ID:
        raise HTTPException(
            status_code=422,
            detail=f"assigned_to_id does not name a known user (id={value})",
        )
    if value == SYSTEM_ACTOR_ID or user.is_system:
        raise HTTPException(
            status_code=422,
            detail=(
                "assigned_to_id may not be the system actor: it is non-loginable, so nothing assigned "
                "to it would ever be read"
            ),
        )
    if not named_user_reaches_project(session, user, project_id, actor=actor):
        raise HTTPException(
            status_code=422,
            detail=(
                "assigned_to_id names a user with no access to this project — a task can only be "
                "assigned to someone who can already reach it. Grant them access to the project first"
            ),
        )
    return value


class TaskCreateSpec(BaseModel):
    """The task-create fields that describe the WORK rather than where it is filed — everything `TaskCreate`
    carries but the project, feature, Jira link and unsaved flag. Its own model because an automation's Task
    action (`app/automation_actions.py`) is a recurring template of exactly this, validated by the same
    `validate_task_spec`, so a field added here validates on both doors.

    `is_plannable`/`priority`/`autonomous` are HIDDEN defaults: an absent value resolves to the
    create-time default, not the column's backfill default, which differs for `is_plannable`."""

    body: str
    # Hidden default true; a child row is only ever created by an epic's plan stage.
    is_plannable: Optional[bool] = None
    # Omitted → column default (0 = normal). Same bound as set-priority: 422 at the edge, never int4 500.
    priority: Optional[int] = Field(default=None, ge=-_PRIORITY_ABS_MAX, le=_PRIORITY_ABS_MAX)
    # Hidden default true.
    autonomous: Optional[bool] = None
    # Opts out of the periodic agentic-maturity/compliance sweep. No hidden default.
    sweep_opt_out: Optional[bool] = None
    # Project memory On/Off (`models.Task.project_memory`); absent/null is Default, resolved at dispatch by
    # `project_memory.switch.effective_memory` — never prefilled here, so the row follows the setting as it
    # stands when it runs.
    project_memory: Optional[bool] = None
    # Validated against `TRANSITION_MODES` (422 otherwise); omitted → `DEFAULT_TRANSITION_MODE`. Read
    # off the PARENT epic at dispatch, so only the epic row's value matters.
    transition_mode: Optional[str] = None
    # A `CREATABLE_PERSONAS` member (422 otherwise); the stored `persona` IS the wire value. Absent, a create
    # takes the project's, else the org's, New-task default (`_default_persona`), never a hidden epic: the column is NOT NULL.
    persona: Optional[str] = None
    # Model pin at create time — same rule as `TaskUpdate.model`, via `_model_pin_or_refuse`. Omitted/empty
    # is INHERIT and stores NULL. Inert on an `epic` (non-dispatched container).
    model: Optional[str] = None
    # Provider pin at create time — same rule as `TaskUpdate.bound_provider`, via `_provider_pin_or_refuse`.
    # Omitted/empty is "Any" and stores NULL. Ignored on an `epic`, which never dispatches (the form hides it).
    bound_provider: Optional[str] = None
    # The SDK half of the one picker (`models.Task.agent_id`), exclusive with `bound_provider` (422 if both).
    # Validated by `_agent_pick_or_422`; ignored on an `epic`, like the provider pin.
    agent_id: Optional[UUID] = None
    # The addressee. Absent → the creating principal (stamped in `new_top_level_task`, so cron-filed rows get
    # it too). Explicit → `_validated_assignee` (422 naming the field). An epic's children inherit it.
    assigned_to_id: Optional[UUID] = None
    # A tenant playbook of the project's org to run (`app/playbook_engine.py`), refused unless the org's
    # plan has Playbooks on. Snapshotted onto the row. One stage: a single row whose `persona`
    # (when given) overrides the stage's expert. Two or more: an `epic` container whose `model` caps every
    # stage; `persona` is ignored. See `_apply_playbook`.
    playbook_id: Optional[UUID] = None


class TaskSpecPins(NamedTuple):
    """What `validate_task_spec` resolved: the model and provider pins to store (None = inherit / any)."""

    model: Optional[str]
    provider: Optional[str]


def validate_task_spec(spec: TaskCreateSpec, principal: Principal | None) -> TaskSpecPins:
    """The field checks of a task create that need no project — body, transition mode, persona, and the model
    and provider pins (authorized, then validated, on PRESENCE of the key: `exclude_unset` makes presence
    readable, absent and null both mean inherit). Shared by `create_task` and the automation Task action, so
    the two cannot drift; every refusal is the create route's own status and sentence."""
    if not spec.body.strip():
        raise HTTPException(status_code=422, detail="body is required")
    # Validated like `update_task`'s edit path (same set, same message); omitted resolves in
    # `_create_epic_with_children`.
    if spec.transition_mode is not None and spec.transition_mode not in TRANSITION_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"transition_mode must be one of {', '.join(TRANSITION_MODES)} "
                f"(got {spec.transition_mode!r})"
            ),
        )
    # Validated like `update_task`'s conversion path; omitted resolves in `create_task` (`_default_persona`).
    if spec.persona is not None and spec.persona not in CREATABLE_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=f"persona must be one of {', '.join(CREATABLE_PERSONAS)} (got {spec.persona!r})",
        )
    provided = spec.model_dump(exclude_unset=True)
    model_pin = _model_pin_or_refuse(spec.model, principal) if "model" in provided else None
    provider_pin = _provider_pin_or_refuse(spec.bound_provider) if "bound_provider" in provided else None
    return TaskSpecPins(model=model_pin, provider=provider_pin)


class TaskCreate(TaskCreateSpec):
    """Create form for a Task. The work itself is `TaskCreateSpec`; this adds where it is filed, how it
    arrives and an optional authored title."""

    # An authored title, kept as sent (trimmed; blank is absent) and never refined: no out-of-band LLM title
    # is enqueued for it. Absent, `create_task` derives one from `body`. Bounded like every title intake.
    title: Optional[str] = Field(default=None, max_length=MAX_TASK_TITLE_LEN)
    # The project this row is filed in, authorized `tasks:create` there. Required unless `feature_id` is
    # given (then the feature's project; both given and disagreeing is a 422).
    project_id: Optional[int] = None
    # The feature this row is filed in; absent is "No feature". A shipped feature is refused. See
    # `_create_feature_or_refuse`.
    feature_id: Optional[UUID] = None
    # The external issue this Task is created FROM. A PAIR, validated together by `_external_issue_or_422`;
    # no hidden default.
    external_task_system: Optional[str] = None
    external_task_id: Optional[str] = None
    # Auto-save: the editor writing a ticket still being typed, not a Save. Lands the row `unsaved`
    # (`models.Task.unsaved`), buying an id and a server-side copy. False for every other caller.
    # Until Save the row gets no LLM title (`UNSAVED_DRAFT_TITLE`), no epic analysis children
    # (`_finalize_unsaved_draft` builds them) and no `activity_events` row.
    unsaved: bool = False
    # Node pin (`models.Task.pinned_node_id`), via `_pinned_node_or_refuse`; absent/null is Any.
    # Not on `TaskCreateSpec`: an automation's Task action pins only through a node target. Ignored on an
    # `epic`, which never dispatches, like the agent pick.
    pinned_node_id: Optional[UUID] = None
    # The one account this task may run on (`models.Task.bound_account_id`), via `_account_pick_or_422`; absent/null
    # is Any. Stamps `bound_provider` from the account. Not on `TaskCreateSpec`, like the node pin. Ignored on an
    # `epic`, which never dispatches.
    account_id: Optional[UUID] = None
    # The template this task is started from (Start or Customize, `app/task_templates.py`): resolved visible and
    # published for the project's org, else 422. It fills every work field the request left out (title, body,
    # persona, autonomous, model); a field sent wins. Not on `TaskCreateSpec`: an automation resolves its template
    # at fire time. With it, `body` may be omitted.
    template_id: Optional[UUID] = None
    body: str = ""


class TaskUpdate(BaseModel):
    """Partial edit — only fields present in the request body are applied. Allowed in EVERY state,
    `active` included (see `update_task`)."""

    title: Optional[str] = None
    body: Optional[str] = None
    is_plannable: Optional[bool] = None
    autonomous: Optional[bool] = None
    sweep_opt_out: Optional[bool] = None
    # Project memory On/Off; explicit null returns the row to Default (see `TaskCreate.project_memory`).
    project_memory: Optional[bool] = None
    # Model pin: top of `dispatch_compose.compose._resolve_model`'s precedence. SUPERADMIN-ONLY (no feature key
    # reaches that access); closed set validated by `app/model_catalog.py`; empty/null = INHERIT.
    model: Optional[str] = None
    # Provider pin, named for its column: dispatch offers only that vendor's accounts. The built-in half of the
    # one agent picker, open to `tasks:edit` (DD13); closed set `app/agent_providers.py::PROVIDER_VALUES`;
    # empty/null = "Any" (unbinds; the next claim re-stamps whichever vendor ran it). Setting it clears
    # `agent_id`.
    bound_provider: Optional[str] = None
    # The account pin (`_account_pick_or_422`); null takes it back to Any.
    account_id: Optional[UUID] = None
    # The SDK half (`models.Task.agent_id`); null clears it, and setting it clears `bound_provider`. Both in one
    # request is a 422 (`_AGENT_OR_PROVIDER`).
    agent_id: Optional[UUID] = None
    # Validated against `TRANSITION_MODES` (422); read off the PARENT epic at dispatch, inert elsewhere.
    transition_mode: Optional[str] = None
    # Epic ⇄ runner-persona conversion, TO `CREATABLE_PERSONAS` from any persona (system ones included).
    # Resending the CURRENT persona is a no-op; a change is allowed in ANY state on a TOP-LEVEL row only
    # and RECONCILES the children, else 400.
    # `Optional` is the partial-edit shape; the column is NOT NULL.
    persona: Optional[str] = None
    # Kept off the default board (`Task.hidden`, also a read gate: `app/authz/visibility.py::visible_task_clause`).
    # Set or clear it on a TOP-LEVEL row, by its creator or a superadmin (403 otherwise); it applies to the
    # WHOLE SUBTREE, as children are born with the epic's value. Ignored on an auto-save. Explicit null is
    # 422, a change on a child 400, a resend of the current value a no-op. See `_apply_hidden`.
    hidden: Optional[bool] = None
    # Move the task AND ITS WHOLE SUBTREE to another project (`project_id` is copied at creation, never
    # derived; a split subtree breaks dispatch, fan-out, sweeps and authz). Resending the CURRENT value is a
    # silent no-op; a change is authorized against the TARGET project and refused while any subtree row is
    # live. Top-level rows only. See `_apply_project_move`.
    project_id: Optional[int] = None
    # File the task AND ITS WHOLE SUBTREE into a feature of its own project, or of the `project_id` in the
    # same request; explicit null is "No feature". Top-level rows only. See `_apply_feature_filing`.
    feature_id: Optional[UUID] = None
    # Explicit null CLEARS its half. The pair rule is judged on the row's RESULTING state, so a partial
    # edit cannot leave a row half-linked (`update_task`, `_external_issue_or_422`).
    external_task_system: Optional[str] = None
    external_task_id: Optional[str] = None
    # The addressee, re-pointable in every state, `done` included (an administrative fact, not a dispatch
    # input). An explicit null is refused by `update_task`; a value goes through `_validated_assignee`
    # against the row's project after any move in the same request.
    assigned_to_id: Optional[UUID] = None
    # Auto-save: not a task field but the kind of write, so `update_task` pops it before walking `fields`.
    # True is the editor's periodic background write; False is a human pressing Save.
    #
    # It buys silence only: columns are written identically, but no `TaskBodyVersion` or `activity_events`
    # row (which would bury both under near-identical rows). Staleness propagation is not silenced (see
    # `update_task`). False is also what finalizes an `unsaved` row (`_finalize_unsaved_draft`).
    autosave: bool = False
    # Pick (a uuid) or drop (explicit null) the playbook, as `TaskCreate.playbook_id`. Only before Save
    # (`unsaved`): a saved row's playbook is fixed, 400. See `_apply_playbook`.
    playbook_id: Optional[UUID] = None
    # The node pin, as `TaskCreate.pinned_node_id`; null clears it (Any). A resend of the current
    # value is a no-op past the operator gate. See `update_task`.
    pinned_node_id: Optional[UUID] = None


# ── Internal helpers ───────────────────────────────────────────────────────────────────────────────
def _snapshot_body_version(
    task: Task, source: str, session: Session
) -> None:
    """Record `task.body` — the value about to be REPLACED — as a version row tagged with the `source`
    causing the replacement. Call BEFORE overwriting `task.body`, never after: a version row is a literal
    prior description, not a log of the new value."""
    session.add(
        TaskBodyVersion(
            task_id=task.id, body=task.body, source=source
        )
    )


#: The `source` an auto-save's snapshot carries while its editing burst is open.
#: `_close_autosave_body_version` promotes it to `manual` on the next explicit save; it survives only on
#: a burst its author abandoned without saving, where "autosave" is the truth.
_BODY_VERSION_SOURCE_AUTOSAVE = "autosave"


def _open_autosave_body_version_id(task_id: int, session: Session) -> Optional[int]:
    """The id of `task_id`'s NEWEST body version when an auto-save wrote it — i.e. an EDITING BURST is
    open and that row already holds the description the burst began from — else None.

    Snapshotting every tick buries the Versions tab; snapshotting none loses the revision. So one row
    is taken on a burst's first tick, holding what a manual Save would have. Only auto-save writes the
    token and an explicit save clears it, so "the newest row is an auto-save" is "a burst is open".
    Ordered by serial `id` (`created_at` can tie)."""
    newest = session.scalars(
        select(TaskBodyVersion)
        .where(TaskBodyVersion.task_id == task_id)
        .order_by(TaskBodyVersion.id.desc())
        .limit(1)
    ).first()
    if newest is None or newest.source != _BODY_VERSION_SOURCE_AUTOSAVE:
        return None
    return newest.id


def _close_autosave_body_version(task_id: int, session: Session) -> None:
    """An explicit save ends the editing burst: the row it opened becomes `manual`, the description this
    operator replaced.

    Called before the save's own writes (see `update_task`). A no-op when no burst is open; call it on
    every explicit save."""
    open_id = _open_autosave_body_version_id(task_id, session)
    if open_id is None:
        return
    session.execute(
        update(TaskBodyVersion)
        .where(TaskBodyVersion.id == open_id)
        .values(source="manual")
    )


# ── CRUD routes ────────────────────────────────────────────────────────────────────────────────────
@router.post("", response_model=TaskOut, status_code=201)
@feature("tasks", "create")
def create_task(
    body: TaskCreate,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Create a top-level `draft` work item — an EPIC with its auto-initialized analysis children, or a
    single top-level task carrying one operator runner persona.

    An epic is a non-dispatched container plus business_analysis/architecture/plan children sequenced by
    `task_dependencies`; `persona='coder'` creates one directly dispatchable row with no children.
    An absent `persona` is the project's, else the org's, New-task default (`_default_persona`), anything outside
    `CREATABLE_PERSONAS` is a 422, and the title is the authored `title` when given, else derived from the body."""
    # A feature, when named, decides the project (a task in a feature is in its project), so it is resolved
    # first; the project's own authorization below then applies unchanged.
    feature_row = _create_feature_or_refuse(body, session, principal)
    project_id = body.project_id
    if feature_row is not None:
        if project_id is not None and project_id != feature_row.project_id:
            raise HTTPException(
                status_code=422, detail=f"feature {feature_row.id} is in another project",
            )
        project_id = feature_row.project_id
    if project_id is None:
        raise HTTPException(status_code=422, detail="project_id is required")
    # `project_id` arrives in the body, so the route guard cannot check it. "Does not exist" and "not
    # yours" are one 422, or the route becomes a scope-existence oracle: `tasks:create` on that project.
    # A project the caller already SEES (`projects:view`, `visible_project_clause`'s rule) leaks
    # nothing, so it is a 403 naming the missing key. In-org only: another org's project (an operator's
    # "All orgs" roster) stays the 422 the web's "Act as" offer reads (`shared.tsx::refusedInOrg`).
    project = session.get(Project, project_id)
    if project is None or not has_project_key(principal, permission_key("tasks", "create"), project_id):
        if (
            project is not None
            and principal is not None
            and project.org_id == principal.org_id
            and has_project_key(principal, PROJECTS_VIEW_KEY, project_id)
        ):
            raise HTTPException(
                status_code=403,
                detail=f'You can\'t create tasks in {project.name}: your role there has no "tasks:create".',
            )
        raise HTTPException(status_code=422, detail="project not found")
    # A system project's tasks are filed by automations only, each pinned (`app/node_targets.py`).
    if project.system_kind is not None:
        raise HTTPException(status_code=422, detail=node_targets.UNPINNED_SYSTEM_TASK)
    # A template fills what the request left out, before any check below reads the body or persona.
    template: Optional[task_templates.AnyTemplate] = None
    template_model: Optional[str] = None
    if body.template_id is not None:
        template = task_templates.startable(session, body.template_id, project.org_id)
        if template is None:
            raise HTTPException(status_code=422, detail=task_templates.UNAVAILABLE_DETAIL)
        body, template_model = _apply_template(body, template)
    # Body, transition mode, persona and the two pins: the checks shared with an automation's Task action.
    model_pin, provider_pin = validate_task_spec(body, principal)
    if template_model is not None:
        # The template's own pin, chosen by its editor: validated, never re-authorized against the starter.
        try:
            model_pin = model_catalog.validate_model(template_model) or None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"model: {exc}") from exc
    # The SDK half of the picker; the built-in half is `provider_pin`.
    if body.agent_id is not None and (provider_pin or body.account_id is not None):
        raise HTTPException(status_code=422, detail=_AGENT_OR_PROVIDER)
    account_pick = _account_pick_or_422(session, body.account_id, principal, project.org_id, provider_pin)
    if account_pick is not None:
        # The account decides the vendor: the pin is stored as its provider, the binding dispatch narrows by.
        provider_pin = account_pick.provider
    _provider_on_plan_or_422(session, provider_pin, project.org_id)
    agent_pick = _agent_pick_or_422(session, body.agent_id, project.org_id)
    node_pin = (
        _pinned_node_or_refuse(session, body.pinned_node_id, principal, project)
        if "pinned_node_id" in body.model_fields_set
        else None
    )

    # A playbook decides the row's shape before anything below reads `body.persona`: complex is an epic
    # container, simple is one row on its stage's persona (`_playbook_plan_or_422`).
    playbook_plan = (
        _playbook_plan_or_422(session, project, body.playbook_id, body.persona, model_pin)
        if body.playbook_id is not None
        else None
    )
    # No persona named and no playbook: the project's, else the org's, New-task default, never a hidden epic (`_default_persona`).
    persona = (
        playbook_plan.persona if playbook_plan is not None
        else body.persona or _default_persona(project)
    )
    body = body.model_copy(update={"persona": persona})

    # An advisor chat also demands `worktrees:edit` on the same project. A chat's deliverable is a console
    # the operator types into, and `worktrees:edit` decides whether it is two-way
    # (`routes/console.py::_console_read_only`); without it the chat would park unanswerably, holding a slot.
    # Additive: only `persona == advisor` reaches it. 403, not the 422 above: the caller already proved they
    # hold the project, so nothing leaks, and it names the missing capability. `has_project_key` is allow-all
    # for `None` and superadmins. Still the project, since a console is delivery.
    if body.persona == PERSONA_ADVISOR and not has_project_key(
        principal, permission_key("worktrees", "edit"), project_id
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "creating a chat requires worktrees:edit on this project — the advisor's deliverable is "
                "a two-way console session, and without that permission the console is read-only"
            ),
        )

    # Capped per project and per operator (H-1), after authorization, so an unauthorized caller cannot learn
    # other people's chat counts.
    _advisor_chat_cap_or_409(
        session,
        project_id=project_id,
        creator_id=principal.user_id if principal is not None else None,
        persona=body.persona,
    )

    # Validated through the edit path's helper, so a bad pair is a clean 422 before any write; builders
    # re-derive the normalized values.
    _external_issue_or_422(body.external_task_system, body.external_task_id)
    # The submitted assignee, against the derived project (the subject the picker listed). `_user`, not
    # `principal`: the probe narrows on the actor, and the principal may be the agent identity (None).
    if body.assigned_to_id is not None:
        _validated_assignee(session, body.assigned_to_id, project_id=project_id, actor=_user)

    # The deterministic title; the LLM title lands out of band (`app/task_title.py`, enqueued after commit),
    # since a thinking-model round trip would cost the create 2-10s. The placeholder is what stacks without
    # a vendor key have always shipped. An auto-save wears `UNSAVED_DRAFT_TITLE` until Save, where
    # `_finalize_unsaved_draft` derives the real one. An authored title beats both and is never refined
    # (`authored_title`); `_finalize_unsaved_draft` replaces only the constant.
    # Whitespace collapsed (a title is one line): a pasted newline or tab never reaches a header or a Slack card.
    authored_title = " ".join((body.title or "").split()) or None
    if authored_title is not None:
        title = authored_title
    else:
        title = UNSAVED_DRAFT_TITLE if body.unsaved else placeholder_title(body.body)
    # The epic is the one special case; every other creatable persona is one top-level row. Branching on
    # `epic` (not on a standalone list) means a newly creatable persona cannot silently become an epic.
    if persona == PERSONA_EPIC:
        return _create_epic_with_children(
            body, title, session, principal, model_pin, project_id, playbook_plan=playbook_plan, template=template,
        )
    return _create_coder_task(
        body, title, session, principal, persona, model_pin, project_id,
        provider=provider_pin, agent_id=agent_pick, playbook_plan=playbook_plan, pinned_node_id=node_pin,
        account_id=account_pick.id if account_pick is not None else None, template=template,
    )


def _apply_template(body: TaskCreate, template: task_templates.AnyTemplate) -> tuple[TaskCreate, Optional[str]]:
    """`body` with every work field it left out (or sent blank) taken from `template`, and the template's model pin
    when the request sent none or the same one — that pin is the template editor's choice, not the starter's, so
    `create_task` applies it without `_model_pin_or_refuse`'s bar. None when the request's own pin stands."""
    sent = body.model_dump(exclude_unset=True)
    if not (body.body or "").strip():
        sent["body"] = template.body
    if not (body.title or "").strip():
        sent["title"] = template.title
    if body.persona is None:
        sent["persona"] = template.persona
    if body.autonomous is None:
        sent["autonomous"] = template.autonomous
    from_template = "model" not in sent or (body.model or None) == (template.model or None)
    if from_template:
        sent.pop("model", None)
    return TaskCreate(**sent), (template.model if from_template else None)


def _link_template(
    session: Session, task: Task, template: Optional[task_templates.AnyTemplate], principal: Principal | None
) -> None:
    """Record `template` as `task`'s source, its name kept for after a delete (D15), copy the images its body names,
    and count the start — only for a person's create (I-2; `principal` None is the agent/infra caller). Called after
    the row's flush, inside the create's transaction."""
    if template is None:
        return
    if isinstance(template, OrgTaskTemplate):
        task.org_template_id = template.id
    else:
        task.template_id = template.id
    task.template_name = template.name
    copy_template_assets(
        session, template_id=task.template_id, org_template_id=task.org_template_id, task=task, body=task.body
    )
    if principal is not None:
        task_templates.record_start(session, template)


def _default_persona(project: Project) -> str:
    """The persona an untouched New-task row would have sent for `project`: `epic` when the org's "Epic" default
    is on, else the project's own `default_persona`, else the org's "Persona" default
    (`operator_settings.defaults.task_create_defaults`, the form's own prefill).
    So an API create that names no persona is what the console would have made, and its response's `persona`
    says so. An `advisor` default is `coder`, as on the inline row (`useInlineTaskCreate.ts`): a create that named
    nothing never silently opens a chat, which holds a slot until someone completes it."""
    from app.operator_settings.defaults import task_create_defaults

    defaults = task_create_defaults(project.org_id)
    if defaults["complex"]:
        return PERSONA_EPIC
    persona = project.default_persona or str(defaults["persona"])
    return persona if persona in CREATABLE_PERSONAS and persona != PERSONA_ADVISOR else PERSONA_CODER


class _PlaybookPlan(NamedTuple):
    """What a playbook makes of a create or an unsaved edit: the snapshot, the persona the row carries, its
    `model` column, and — simple only — the stage the row itself runs."""

    snapshot: dict[str, Any]
    persona: str
    model: Optional[str]
    stage: Optional[dict[str, Any]]


def _playbooks_on_or_422(session: Session, org_id: Optional[UUID]) -> None:
    """422 `playbook_engine.OFF_DETAIL` when `org_id`'s plan has Playbooks off: a snapshot already held (a draft's,
    a container's) is no licence to keep running one."""
    if not playbook_engine.enabled(session, org_id):
        raise HTTPException(status_code=422, detail=playbook_engine.OFF_DETAIL)


def _playbook_plan_or_422(
    session: Session, project: Project, playbook_id: UUID, persona: Optional[str], model: Optional[str],
) -> _PlaybookPlan:
    """Resolve `playbook_id` for a row in `project` (`playbook_engine.load` refuses an org whose plan has
    Playbooks off, another org's playbook and one the engine cannot run — all 422).

    Complex: an `epic` container; `persona` is ignored and `model` is the cap the stages are lowered to.
    Simple: the row runs stage 0 — `persona`, when a creatable non-epic one, overrides its expert, and
    `model`, when pinned, overrides its tier (the stage's tier is the stage default at dispatch,
    `dispatch_compose.compose.compose`, so nothing writes it into the pin)."""
    try:
        snap = playbook_engine.load(session, project.org_id, playbook_id)
    except playbook_engine.PlaybookNotRunnable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _plan_from_snapshot(snap, persona, model)


def _plan_from_snapshot(snap: dict[str, Any], persona: Optional[str], model: Optional[str]) -> _PlaybookPlan:
    """`_playbook_plan_or_422` on a snapshot already held — the unsaved-edit path re-resolves from the row's
    own copy, so an autosave never depends on the live playbook. A `persona` override must be a creatable
    one, and the stage it picks must still run alone (it can swap the deliverable)."""
    if not playbook_engine.is_simple(snap):
        return _PlaybookPlan(snapshot=snap, persona=PERSONA_EPIC, model=model, stage=None)
    override = persona if persona is not None and persona != PERSONA_EPIC else None
    if override is not None and override not in CREATABLE_PERSONAS:
        raise HTTPException(
            status_code=422,
            detail=f"persona must be one of {', '.join(CREATABLE_PERSONAS)} (got {override!r})",
        )
    try:
        expert = playbook_engine.expert_for_persona(override) if override is not None else None
        stage = playbook_engine.resolve_stage(snap, 0, expert=expert)
        playbook_engine.check_alone(stage)
    except playbook_engine.PlaybookNotRunnable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _PlaybookPlan(snapshot=snap, persona=stage["persona"], model=model, stage=stage)


def _create_feature_or_refuse(
    body: TaskCreate, session: Session, principal: Principal | None
) -> Optional[Feature]:
    """The feature a create files its row into, or None when it names none. Refuses before any write.

    Visibility is the feature's own (`tasks:view` on its project); the create key (`tasks:create`) is then
    asked of that project by `create_task`'s ordinary gate, so a feature adds no second rule. "Does not
    exist" and "not yours" are one 422. A shipped feature is closed for filing (400, the filing door's
    sentence)."""
    if body.feature_id is None:
        return None
    # `FOR SHARE`: a concurrent ship or delete waits for this create to commit, so the row cannot land in a
    # feature shipped or deleted between this check and the insert.
    feature_row = session.get(Feature, body.feature_id, with_for_update={"read": True})
    if feature_row is None or not can_access(
        session, principal, "feature", body.feature_id, mode=ObjectAccess.READ
    ):
        raise HTTPException(status_code=422, detail=f"feature {body.feature_id} not found")
    if feature_row.status == FEATURE_SHIPPED:
        raise HTTPException(status_code=400, detail=_FEATURE_SHIPPED_DETAIL)
    return feature_row


# The analysis children an epic is auto-initialized with, in dependency order. Wiring is a
# FAN-IN DAG: each child depends on EVERY earlier child, not just its predecessor, so `plan` receives
# every earlier stage's settled output directly. Single home for the epic's child recipe.
_EPIC_ANALYSIS_CHILDREN: tuple[str, ...] = (
    PERSONA_BUSINESS_ANALYSIS,
    PERSONA_ARCHITECTURE,
    PERSONA_PLAN,
)


def _create_epic_children(epic: Task, session: Session) -> list[Task]:
    """An epic's children at Save: a playbook container's start stages, else the analysis recipe.

    Every child is the EPIC's: `created_by_id` is copied from the epic, never taken from whoever's request
    triggered the fan-out — an operator saving, starting or converting a member's epic stays out of the
    children's authorship. No `started_by_id`: a child inherits its epic's starter through the walk
    (`task_ancestry.starter_user_id`)."""
    if epic.playbook:
        return _create_playbook_start_children(epic, session)
    return _create_analysis_children(epic, session)


def _new_stage_child(
    epic: Task, stage: dict[str, Any], *, title: str, body: str, created_by_id: Optional[UUID],
    phase_index: Optional[int] = None, phase_name: Optional[str] = None,
) -> Task:
    """One stage row of a playbook container (`playbook_engine.resolve_stage`), constructed but not added.

    Like an analysis child it inherits visibility, priority and the addressee; unlike one it carries its
    stage (whose tier, already capped by the container's `model`, is its stage default at dispatch) and its
    own Full Auto (`playbook_engine.effective_autonomous`)."""
    return Task(
        project_id=epic.project_id,
        # The epic's org, never the scope's: `fk_tasks_project_id_org` holds a task to its project's org.
        org_id=epic.org_id,
        parent_task_id=epic.id,
        source=task_source.source_for(epic, task_source.CONSOLE),
        hidden=epic.hidden,
        title=title,
        body=body,
        is_plannable=False,
        state=DRAFT,
        persona=stage["persona"],
        playbook_stage=stage,
        autonomous=playbook_engine.effective_autonomous(stage, epic.autonomous),
        priority=epic.priority,
        phase_index=phase_index,
        phase_name=phase_name,
        created_by_id=created_by_id,
        assigned_to_id=epic.assigned_to_id,
    )


def _stage_edge(session: Session, row: Task, prereq: Task) -> None:
    """`row` waits on `prereq`: a duplicate is a no-op, a cycle rolls the whole build back (422)."""
    if session.get(TaskDependency, {"task_id": row.id, "depends_on_task_id": prereq.id}) is not None:
        return
    if _would_create_cycle(row.id, prereq.id, session):
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail=f"playbook stage edge {row.id} -> {prereq.id} would create a cycle — nothing was filed",
        )
    session.add(TaskDependency(task_id=row.id, depends_on_task_id=prereq.id))


def _wire_stage_edges(
    session: Session, snap: dict[str, Any], rows_by_index: dict[int, list[Task]], new_indexes: Sequence[int],
    *, extra_prereqs: Sequence[Task] = (),
) -> None:
    """Wire the stages just filed (`new_indexes`, every row already flushed) into the container's graph.

    Each new row waits on every row of each stage it depends on — earlier or later in the list, since all
    were inserted first — plus `extra_prereqs`. A stage a Plan has not filed yet stands in as its Plan
    stage's rows, so nothing runs ahead of work that does not exist yet; and when that Plan files it, the
    rows still waiting (`draft`/`ready`) that depend on it gain the edges they were promised."""
    def prereqs_of(index: int) -> list[Task]:
        out: list[Task] = []
        for dep in playbook_engine.dependency_indexes(snap, index):
            rows = rows_by_index.get(dep)
            if not rows:
                creator = playbook_engine.creator_index(snap, dep)
                rows = rows_by_index.get(creator, []) if creator is not None else []
            out += [row for row in rows if row not in out]
        return out

    for index in new_indexes:
        prereqs = prereqs_of(index) + [row for row in extra_prereqs]
        for row in rows_by_index[index]:
            for prereq in prereqs:
                if prereq is not row:
                    _stage_edge(session, row, prereq)
    filed = set(new_indexes)
    for index, rows in rows_by_index.items():
        waits_on_new = filed & set(playbook_engine.dependency_indexes(snap, index))
        if index in filed or not waits_on_new:
            continue
        for row in rows:
            if row.state not in (DRAFT, READY):
                continue
            for dep in waits_on_new:
                for prereq in rows_by_index[dep]:
                    _stage_edge(session, row, prereq)


def _create_playbook_start_children(epic: Task, session: Session) -> list[Task]:
    """Fan a playbook container out into the stages created at start, each a `draft` child wired to the
    stages it waits on — `_create_analysis_children`'s counterpart, same transaction rules (flushed, not
    committed). Stages a Plan stage creates are filed by `append_tasks` when that Plan settles.

    A start stage's work item is the container's body, composed live at dispatch like an analysis child's
    (`_compose_dispatch_body`)."""
    _playbooks_on_or_422(session, epic.org_id)
    snap = epic.playbook or {}
    rows_by_index: dict[int, list[Task]] = {}
    for index in playbook_engine.start_stage_indexes(snap):
        stage = playbook_engine.resolve_stage(snap, index, cap=epic.model)
        child = _new_stage_child(
            epic, stage,
            title=derived_child_title(stage["persona"], epic.title),
            body=_DISPATCH_COMPOSED_BODY,
            created_by_id=epic.created_by_id,
        )
        session.add(child)
        rows_by_index[index] = [child]
    session.flush()   # every id first, so a stage may wait on one listed after it
    _wire_stage_edges(session, snap, rows_by_index, list(rows_by_index))
    return [row for rows in rows_by_index.values() for row in rows]


def sync_epic_child_autonomous(session: Session, epic: Task) -> None:
    """Restamp `epic`'s interviewing children (`INTERVIEWING_CHILD_PERSONAS`) with its `autonomous`, so their
    stored column equals the effective one dispatch reads (`_task_effective_autonomous`) and question
    analytics over the column are not misleading. A no-op for a non-epic. Does not commit."""
    if epic.persona != PERSONA_EPIC:
        return
    for child in session.scalars(
        select(Task).where(
            Task.parent_task_id == epic.id,
            Task.persona.in_(INTERVIEWING_CHILD_PERSONAS),
            Task.playbook_stage.is_(None),
        )
    ):
        child.autonomous = epic.autonomous


def _create_analysis_children(epic: Task, session: Session) -> list[Task]:
    """Fan `epic` out into its three draft analysis children plus the fan-in dependency edges — the single
    home for the recipe, so `update_task`'s `coder → epic` conversion builds the identical shape.

    `epic` must already be flushed; each child is `draft` with `_DISPATCH_COMPOSED_BODY`, each edge
    cycle-vetted, flushed but not committed ("one commit, no partial epic"). Each child takes the epic's
    `created_by_id` (`_create_epic_children`) and sits in the epic's project."""
    prior_child_ids: list[int] = []
    children: list[Task] = []
    for persona in _EPIC_ANALYSIS_CHILDREN:
        child = Task(
            project_id=epic.project_id,
            # The epic's org, never the scope's: `fk_tasks_project_id_org` holds a task to its project's org.
            org_id=epic.org_id,
            parent_task_id=epic.id,
            # The epic's origin, so a source filter shows the whole pipeline (`app/task_source.py`).
            source=task_source.source_for(epic, task_source.CONSOLE),
            # Visibility is inherited: the child's title carries the parent's verbatim. `_apply_hidden` keeps
            # the subtree agreeing afterwards.
            hidden=epic.hidden,
            # One home for `[<persona>] <epic title>` (`app/task_title.py`): the auto-title refinement recognises
            # this exact string, so a drift would silently match nothing.
            title=derived_child_title(persona, epic.title),
            # A placeholder: the work item is composed live from the epic at dispatch (`_compose_dispatch_body`).
            body=_DISPATCH_COMPOSED_BODY,
            is_plannable=False,
            state=DRAFT,
            persona=persona,
            priority=epic.priority,
            # The switch this child will actually dispatch with (`_task_effective_autonomous`), stored so the
            # column never claims Full Auto on a child that inherits the epic's OFF; `sync_epic_child_autonomous`
            # keeps it true when the epic's switch moves.
            autonomous=epic.autonomous if persona in INTERVIEWING_CHILD_PERSONAS else True,
            # The epic's author, never the request's principal; PROVENANCE ONLY — a Task carries no grant row.
            created_by_id=epic.created_by_id,
            # The epic's own addressee, never the finalizing principal's (who may differ).
            assigned_to_id=epic.assigned_to_id,
        )
        session.add(child)
        session.flush()   # assign child.id so it can declare its dependency edges below
        # Fan in to every earlier child, so a later stage receives every earlier settled output; the cycle
        # guard is defensive.
        for prior_id in prior_child_ids:
            if _would_create_cycle(child.id, prior_id, session):
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"epic child dependency {child.id} -> {prior_id} would create a cycle "
                        "(unexpected for a fresh epic — aborting create)"
                    ),
                )
            session.add(
                TaskDependency(task_id=child.id, depends_on_task_id=prior_id)
            )
        prior_child_ids.append(child.id)
        children.append(child)
    return children


def new_top_level_task(
    *,
    project_id: int,
    title: str,
    body: str,
    persona: str,
    created_by_id: Optional[UUID],
    source: str,
    priority: Optional[int] = None,
    autonomous: bool = True,
    sweep_opt_out: bool = False,
    transition_mode: str = DEFAULT_TRANSITION_MODE,
    external_task_system: Optional[str] = None,
    external_task_id: Optional[str] = None,
    model: Optional[str] = None,
    hidden: bool = False,
    unsaved: bool = False,
    assigned_to_id: Optional[UUID] = None,
    feature_id: Optional[UUID] = None,
    pinned_node_id: Optional[UUID] = None,
) -> Task:
    """The row-building core of the operator create path — one top-level, directly-dispatchable `draft`
    Task, constructed but not added, flushed or committed. The caller owns the transaction.

    Shared by `_create_coder_task` (REST) and `app/automation_fire.py`, so the load-bearing
    constants (`is_plannable=False`, `state=DRAFT`) live in one place. Not `create_system_task` (no
    `model`/`autonomous`, always hidden). `hidden` defaults false; a Runner cron may file hidden work
    (see `app/authz/visibility.py::visible_task_clause` for what a hidden row's creator reaches).

    `is_plannable` is False: with no children, `True` would show a false "done with no subtasks"
    warning. `created_by_id` is authorship (hidden-row read arm, the author-draft widening), never
    a grant. `model` is normalized by `app/model_catalog.py`; None inherits. `source` is the filer's
    origin (`app/task_source.py`), required so no new filer can forget it.

    `pinned_node_id` confines placement to that node. Two filers pass it: the node-target filer
    (`app/node_targets.py`), where it is also what lets the row into the org's system Nodes project
    (`node_targets.prepare`), and `create_task` for an operator's pin (`_pinned_node_or_refuse`).
    """
    return Task(
        project_id=project_id,
        source=task_source.validate_source(source),
        title=title,
        body=body,               # the caller's own text — this row IS the work item
        is_plannable=False,      # LOAD-BEARING, see the docstring — never the hidden True default
        state=DRAFT,
        persona=persona,         # already validated by the caller against its own gate
        hidden=hidden,           # operator-visible by default; see the docstring for the one producer
                                 # that opts a row out of the board's default view
        priority=priority,
        autonomous=autonomous,
        sweep_opt_out=sweep_opt_out,
        transition_mode=transition_mode,
        external_task_system=external_task_system,
        external_task_id=external_task_id,
        model=model,
        # Still being typed (`models.Task.unsaved`). A parameter like `hidden`: only the editor's auto-save
        # passes True.
        unsaved=unsaved,
        created_by_id=created_by_id,
        # The addressee defaults to the author here, so cron-filed rows get the same answer. A NULL creator stays
        # a NULL assignee. Explicit values arrive validated (`TaskCreate.assigned_to_id`).
        assigned_to_id=assigned_to_id if assigned_to_id is not None else created_by_id,
        # Already resolved against `project_id` by the caller (`_create_feature_or_refuse`); None is
        # "No feature".
        feature_id=feature_id,
        pinned_node_id=pinned_node_id,
    )


def _create_coder_task(
    body: TaskCreate, title: str, session: Session, principal: Principal | None, persona: str,
    model: Optional[str], project_id: int, *, provider: Optional[str] = None,
    agent_id: Optional[UUID] = None, playbook_plan: Optional[_PlaybookPlan] = None,
    pinned_node_id: Optional[UUID] = None, account_id: Optional[UUID] = None,
    template: Optional[task_templates.AnyTemplate] = None,
) -> TaskOut:
    """Build a single top-level row carrying one of the `OPERATOR_PERSONAS` — ONE row, ZERO children.

    The non-epic counterpart of `_create_epic_with_children`: a `draft` row with no parent, not hidden,
    carrying the operator's own body. `persona` is a parameter (validated against `CREATABLE_PERSONAS`);
    a top-level analysis row is safe because `_dispatch_work_item` falls back to its own body.

    The row is built by `new_top_level_task`; this is the REST arm around it (request defaults, event,
    projection)."""
    # The server decides the persona's default for `autonomous`; a submitted value wins, and
    # personas declaring nothing keep `True`.
    autonomous = create_autonomous_for_persona(persona, body.autonomous)
    sweep_opt_out = body.sweep_opt_out if body.sweep_opt_out is not None else False
    # Already validated in `create_task`; an omitted mode resolves to the constant, the column's own default.
    transition_mode = (
        body.transition_mode if body.transition_mode is not None else DEFAULT_TRANSITION_MODE
    )
    # Already validated in `create_task`; called again for the NORMALISED pair (blank → null).
    external_system, external_id = _external_issue_or_422(
        body.external_task_system, body.external_task_id
    )
    task = new_top_level_task(
        # The derived project, handed down so the FK-checked pair is the one authorized.
        project_id=project_id,
        source=task_source.CONSOLE,
        title=title,
        body=body.body,          # the operator's own text — this row IS the work item
        persona=persona,         # the operator's Persona choice, already validated by `create_task`
        priority=body.priority,
        autonomous=autonomous,
        sweep_opt_out=sweep_opt_out,
        transition_mode=transition_mode,
        external_task_system=external_system,
        external_task_id=external_id,
        # Already authorized and normalized by `create_task`; None inherits.
        model=model,
        # The editor's auto-save, straight off the request — `TaskCreate.unsaved` is the single home.
        unsaved=body.unsaved,
        # Authorship (hidden-row read arm, the author-draft widening); access is decided on the project.
        # See `models.Project.created_by_id`.
        created_by_id=principal.user_id if principal is not None else None,
        # Already validated by `create_task` when present; None takes the builder's author default.
        assigned_to_id=body.assigned_to_id,
        feature_id=body.feature_id,
        # Already authorized and validated by `create_task`; None is Any.
        pinned_node_id=pinned_node_id,
    )
    # Already validated by `create_task`; None is "Any" (unbound until first claim). At most one is set.
    task.bound_provider = provider
    task.agent_id = agent_id
    task.bound_account_id = account_id
    task.project_memory = body.project_memory
    # A one-stage playbook: this row IS its stage (`_PlaybookPlan`).
    if playbook_plan is not None:
        task.playbook = playbook_plan.snapshot
        task.playbook_stage = playbook_plan.stage
    session.add(task)
    session.flush()
    _link_template(session, task, template, principal)
    # No object grant: the create gate required `tasks:create` here and `visible_task_clause` reads
    # `tasks:view` on the same scope. For an auto-save, notify but do not record: the author's board must
    # show the row (the relay probes visibility per subscriber), but the "created" row waits for Save
    # (`_finalize_unsaved_draft`).
    emit_console_event(
        session, entity="task", id=task.id, action="created", record_transition=not body.unsaved,
    )
    session.commit()
    # After the commit: the workflow re-reads the row, and an early dequeue would find nothing and burn the
    # one enqueue. Never raises. Deferred import: the queue module pulls `dbos` and registers a workflow.
    from app.durable.task_title_queue import enqueue_title

    # Not while the author is typing; Save enqueues it (`_finalize_unsaved_draft`). Never over an authored title.
    if not body.unsaved and not (body.title or "").strip():
        enqueue_title(task.id)
    _announce_assignment_after_create(task, body, principal)
    # `child_count=0` explicitly (else null). `actor_names` explicitly rather than `session=session`, which
    # would also compute the counts above; without a map the author's name would render as an em-dash.

    # `authored_draft` explicitly (no session, so no probe), resolved through the clause rather than asserted
    # from what was just written, which would go stale.
    return _task_out(
        task,
        child_count=0,
        done_child_count=0,
        actor_names=actors.actor_names(
            session, (task.created_by_id, task.updated_by_id, task.assigned_to_id), name_acting=True
        ),
        # `org_labels` explicitly (no session): otherwise `org=null`, which the board would spread over the row.
        # Empty only when the creator has no org (`app/tenancy/labels.py`).
        org_labels=org_refs(session, [task.org_id]),
        authored_draft=task.id in _authored_draft_ids([task.id], session, principal),
        # Same reason: resolved through the clause.
        may_edit=task.id in _may_edit_ids([task.id], session, principal),
    )


def _announce_assignment_after_create(
    task: Task, body: TaskCreate, principal: Principal | None
) -> None:
    """The "assigned to you" notice a CREATE owes, after its commit — one home for the coder and epic arms.

    Not on an auto-save (it fires at finalize, in `update_task`), and not to yourself (the default
    assignee is the creator). `task_notify.announce_assignment` is best-effort, so a failed DM never
    fails a create; imported lazily like the title queue."""
    if body.unsaved or task.assigned_to_id is None:
        return
    creator_id = principal.user_id if principal is not None else None
    if task.assigned_to_id == creator_id:
        return
    from app import task_notify

    task_notify.announce_assignment(
        task.id, assignee_id=task.assigned_to_id, assigned_by_id=creator_id
    )


def _create_epic_with_children(
    body: TaskCreate, title: str, session: Session, principal: Principal | None,
    model: Optional[str], project_id: int, *, playbook_plan: Optional[_PlaybookPlan] = None,
    template: Optional[task_templates.AnyTemplate] = None,
) -> TaskOut:
    """Build an auto-initialized epic and its analysis children in ONE transaction, so a
    mid-build failure leaves NO partial epic:
      1. the **epic** — a `Task(persona='epic', state='draft')` with the generated title plus the
         human's body/priority and autonomous-mode fields, each with its hidden create-time default. A
         non-dispatched container: `epic` is absent from PERSONA_CHARTER;
      2. its three **analysis children** plus their fan-in edges, from `_create_analysis_children`.
    Returns the epic with its children attached; it holds `draft` until started."""
    # Hidden create-time defaults: an explicit request value wins; `is_plannable`'s create default differs
    # from its column default.
    is_plannable = body.is_plannable if body.is_plannable is not None else True
    autonomous = body.autonomous if body.autonomous is not None else True
    sweep_opt_out = body.sweep_opt_out if body.sweep_opt_out is not None else False
    # Already validated in `create_task`; an omitted mode resolves to the constant, the column's own default.
    transition_mode = (
        body.transition_mode if body.transition_mode is not None else DEFAULT_TRANSITION_MODE
    )
    # The external issue lands on the epic only: its children are stages of one issue, and copying the id
    # down would multiply Jira done-comments. Already validated in `create_task`.
    external_system, external_id = _external_issue_or_422(
        body.external_task_system, body.external_task_id
    )
    epic = Task(
        project_id=project_id,
        source=task_source.CONSOLE,
        # Its stage tasks take it from the epic in the database's resolve trigger on insert, so the fan-out
        # code never names a feature.
        feature_id=body.feature_id,
        title=title,
        body=body.body,
        is_plannable=is_plannable,
        state=DRAFT,
        persona=PERSONA_EPIC,    # non-dispatched container — never runs (absent from PERSONA_CHARTER)
        priority=body.priority,
        autonomous=autonomous,
        sweep_opt_out=sweep_opt_out,
        # The epic's own; its children stay null and inherit it at dispatch (`switch.effective_memory`).
        project_memory=body.project_memory,
        # Only the EPIC row takes it: dispatch reads the mode off the PARENT epic, so a child's is inert.
        transition_mode=transition_mode,
        # Stored though an epic never dispatches, so the submitted value reads back; see `TaskCreate.model`.
        model=model,
        external_task_system=external_system,
        external_task_id=external_id,
        # The editor's auto-save — see `_create_coder_task`'s own pass-through and `TaskCreate.unsaved`.
        unsaved=body.unsaved,
        # PROVENANCE ONLY, like every other create path — see `Task.created_by_id`.
        created_by_id=principal.user_id if principal is not None else None,
        # The addressee: submitted (validated) else the author; inherited by every child.
        assigned_to_id=(
            body.assigned_to_id
            if body.assigned_to_id is not None
            else (principal.user_id if principal is not None else None)
        ),
    )
    # A complex playbook: the container keeps the snapshot its stage children are built from.
    if playbook_plan is not None:
        epic.playbook = playbook_plan.snapshot
    session.add(epic)
    session.flush()   # assign epic.id for the children's parent_task_id + the dependency edges
    _link_template(session, epic, template, principal)
    # No object grant: every subtree row sits in the same project, read via `tasks:view` there.

    # ── An unsaved epic draft is one row ────────────────────────────────────────────────────────────────
    # Owner's ruling: the editor can switch between Complex and simple before Save, so no children are
    # fanned out per tick. `_finalize_unsaved_draft` builds them at Save through the same recipe;
    # `_apply_persona_conversion` holds the matching line for conversions while unsaved.
    children = None if epic.unsaved else _create_epic_children(epic, session)

    # Notify-but-do-not-record for an auto-save — `_create_coder_task` carries the whole argument.
    emit_console_event(
        session, entity="task", id=epic.id, action="created", record_transition=not epic.unsaved,
    )
    session.commit()
    # The epic alone: its children's titles derive from it, and the refinement re-stamps them. After commit,
    # deferred — see `_create_coder_task`.
    from app.durable.task_title_queue import enqueue_title

    # Withheld while unsaved or authored, for `_create_coder_task`'s reasons exactly.
    if not body.unsaved and not (body.title or "").strip():
        enqueue_title(epic.id)
    _announce_assignment_after_create(epic, body, principal)
    # `child_count=0` explicitly on the unsaved arm; `children=None` would trigger a counting query.
    if children is None:
        return _task_out(
            epic, child_count=0, done_child_count=0, session=session, principal=principal,
        )
    return _task_out(epic, children=children, session=session, principal=principal)


# One sentence for "this feature is closed for filing", shared by the create and the filing doors.
_FEATURE_SHIPPED_DETAIL = "feature is shipped — reopen it to file tasks"


# ── Body-version history (item 6a) ────────────────────────────────────────────────────────────────
class TaskBodyVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    body: str
    source: str
    restored_from_id: Optional[int]
    created_at: datetime


@router.get("/{task_id}/body-versions", response_model=list[TaskBodyVersionOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def list_body_versions(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[TaskBodyVersionOut]:
    """Timestamped prior descriptions for `task_id`, newest first."""
    get_task_or_404(task_id, session)
    rows = session.scalars(
        select(TaskBodyVersion)
        .where(TaskBodyVersion.task_id == task_id)
        .order_by(TaskBodyVersion.id.desc())
    ).all()
    return [TaskBodyVersionOut.model_validate(row) for row in rows]
