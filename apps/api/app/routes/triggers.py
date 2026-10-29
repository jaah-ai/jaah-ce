"""The `task_triggers` writers and the event child's operations, shared by `/api/automations`
(`routes/automations.py`, `automation_service`) and the agent verb (`routes/agent_triggers.py`), plus the two
console routes on `/api/triggers`: the unmatched-message log and the state document.

Authorization is org-admin: `@org_admin(Tenancy.CALLER_PLANE)` on each route here and on each `/api/automations`
handler that calls an operation here. An Org Owner/Admin is bound to
their own org by the seam, so another org's trigger or project reads as absent (404); the operator works every
org's. Validation is total and server-side, runs on the merged row, and every refusal names the field.

The secret: create and rotate answer it once; a reveal answers it once more only while no reveal has happened
(an advisor-created trigger's path to a human); no read ever carries it. The audit middleware blanks the
`secret` key of every captured body (`user_activity.SEGMENT_TERMS`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import (
    automation_service,
    ce_caps,
    edition,
    edition_features,
    credential_consumers,
    forges,
    llm_client,
    plan_terms,
    project_repos,
    trigger_health,
    trigger_intake,
    trigger_secrets,
)
from app.operator_settings import reads as settings_reads
from app.actors import actor_names, stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.authz.principal import principal_for_user
from app.authz.visibility import usable_credential_clause
from app.config import get_settings
from app.db import get_session
from app.entity_shape import add_with_org_slug, get_or_404, iso_or_none, new_uuid7, StrictModel
from app.model_catalog import validate_model
from app.models.automations import TaskTrigger, TriggerDedup, TriggerDelivery
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.notifications import InboundEmail
from app.models.projects import Project
from app.models.tasks import Task
from app.persona_registry import TRIGGER_PERSONAS
from app.project_identity import project_or_404
from app.routes.crons import _reject, _via
from app.tenancy.ids import ORG_ZERO_ID
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
from app.trigger_config import SourceConfigError, user_scope_of, validate_source_config
from app.trigger_intake import redact_headers, slack_channel_held_by_another_org, trigger_owner
from app.trigger_template import TemplateError, render, render_title, validate_template
from app.triggers import (
    AUTH_API_KEY,
    DEFAULT_AUTO_PAUSE_FAILURES,
    DEFAULT_CONCURRENCY_CAP,
    DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    DEFAULT_QUEUE_DEPTH,
    DEFAULT_RATE_LIMIT_PER_HOUR,
    DELIVERY_STATES,
    HTTP_SOURCE_KINDS,
    MAX_CONCURRENCY_CAP,
    MAX_PREFLIGHT_TIMEOUT_SECONDS,
    MAX_QUEUE_DEPTH,
    MAX_RATE_LIMIT_PER_HOUR,
    MAX_STATE_BYTES,
    MIN_PREFLIGHT_TIMEOUT_SECONDS,
    OUTCOME_LABELS,
    OUTCOMES,
    PREFLIGHT_KINDS,
    PREFLIGHT_MODEL,
    PREFLIGHT_SCRIPT,
    SOURCE_GITHUB,
    SOURCE_GMAIL,
    SOURCE_KINDS,
    SOURCE_SLACK,
    SOURCE_USER_REGISTERED,
    SOURCE_WEBHOOK,
    UNMATCHED,
    USER_SCOPE_PLATFORM,
)
from app.user_activity import record_write

def _no_such_project(project_id: object) -> str:
    """One detail for every miss, so a foreign project is indistinguishable from a missing one (AC-2.4)."""
    return f"project_id: no project with id {project_id}"


router = APIRouter(prefix="/api/triggers", tags=["triggers"])

NO_HOOKS_ADDRESS = "This control plane has no public hooks address"
#: The community edition takes no event: it listens on loopback, and runs no mail, Slack or sign-up (`write_trigger_create`).
NO_EVENTS_LOCALLY = "Jaah Community Edition runs automations on a schedule."
SECRET_ALREADY_SHOWN = "The secret was already shown once — rotate it to get a new one."
NO_SECRET = "This trigger has no secret: a webhook takes an API key, and only GitHub triggers are signed."
LEGACY_AUTH_REFUSED = "auth: a webhook takes an API key (api_key); a signing secret or bearer token is no longer offered"

DELIVERIES_DEFAULT_LIMIT = 50
DELIVERIES_MAX_LIMIT = 200
MAX_TEMPLATE_CHARS = 20_000
MAX_TITLE_TEMPLATE_CHARS = 500
# numeric(10,4): the largest budget the column holds.
MAX_BUDGET_USD = Decimal("999999")
# A secret response is never cached by a browser or a proxy.
_NO_STORE = {"Cache-Control": "no-store"}
# The credential kind a model preflight runs on (architecture AD-I4).
_PREFLIGHT_CREDENTIAL_TYPE = "anthropic_api_key"


def _hooks_base_url() -> str:
    return get_settings().hooks_public_base_url


# ── Wire models ───────────────────────────────────────────────────────────────────────────────────────


class TriggerCreateFields(StrictModel):
    """The fields a console create and an agent create (`routes/agent_triggers.py`) share."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    source_kind: str
    source_config: dict[str, Any] = Field(default_factory=dict)
    persona: str
    title_template: Optional[str] = Field(default=None, max_length=MAX_TITLE_TEMPLATE_CHARS)
    prompt_template: str = Field(min_length=1, max_length=MAX_TEMPLATE_CHARS)
    model: Optional[str] = None
    full_auto: bool = True
    hidden: bool = False
    preflight_kind: str = "none"
    preflight_script_path: Optional[str] = Field(default=None, max_length=500)
    preflight_prompt: Optional[str] = Field(default=None, max_length=MAX_TEMPLATE_CHARS)


class TriggerCreate(TriggerCreateFields):
    project_id: int
    preflight_credential_id: Optional[UUID] = None
    preflight_timeout_seconds: int = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP
    queue_depth: int = DEFAULT_QUEUE_DEPTH
    rate_limit_per_hour: int = DEFAULT_RATE_LIMIT_PER_HOUR
    budget_usd: Optional[Decimal] = None
    auto_pause_failures: Optional[int] = DEFAULT_AUTO_PAUSE_FAILURES
    auto_pause_idle_days: Optional[int] = None


class TriggerUpdateFields(StrictModel):
    """The fields a console update and an agent update share; only those sent change."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    source_config: Optional[dict[str, Any]] = None
    persona: Optional[str] = None
    title_template: Optional[str] = Field(default=None, max_length=MAX_TITLE_TEMPLATE_CHARS)
    prompt_template: Optional[str] = Field(default=None, min_length=1, max_length=MAX_TEMPLATE_CHARS)
    model: Optional[str] = None
    full_auto: Optional[bool] = None
    hidden: Optional[bool] = None
    preflight_kind: Optional[str] = None
    preflight_script_path: Optional[str] = Field(default=None, max_length=500)
    preflight_prompt: Optional[str] = Field(default=None, max_length=MAX_TEMPLATE_CHARS)


class TriggerUpdate(TriggerUpdateFields):
    """Every field optional; only those sent change. `source_kind` is immutable after create."""

    project_id: Optional[int] = None
    source_kind: Optional[str] = None
    preflight_credential_id: Optional[UUID] = None
    preflight_timeout_seconds: Optional[int] = None
    concurrency_cap: Optional[int] = None
    queue_depth: Optional[int] = None
    rate_limit_per_hour: Optional[int] = None
    budget_usd: Optional[Decimal] = None
    auto_pause_failures: Optional[int] = None
    auto_pause_idle_days: Optional[int] = None


# Fields a PATCH may set to null on purpose (`None` means "clear"), as opposed to "not sent".
_NULLABLE_FIELDS = frozenset({
    "title_template", "model", "preflight_script_path", "preflight_prompt", "preflight_credential_id",
    "budget_usd", "auto_pause_failures", "auto_pause_idle_days",
})


class Health(BaseModel):
    state: str
    reason: Optional[str] = None


class ActorRef(BaseModel):
    id: Optional[UUID] = None
    name: Optional[str] = None


class TriggerOut(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str
    org: Optional[OrgRef] = None
    project_id: int
    project_name: Optional[str] = None
    source_kind: str
    source_config: dict[str, Any]
    # The public URL a sender posts to (webhook/GitHub), or None when this kind has none or this control plane
    # has no hooks address.
    url: Optional[str] = None
    # Whether the secret's one reveal has happened; the secret itself is never on a read.
    has_secret: bool
    secret_revealed: bool
    persona: str
    title_template: Optional[str] = None
    prompt_template: str
    model: Optional[str] = None
    full_auto: bool
    hidden: bool
    preflight_kind: str
    preflight_script_path: Optional[str] = None
    preflight_prompt: Optional[str] = None
    preflight_credential_id: Optional[UUID] = None
    preflight_timeout_seconds: int
    concurrency_cap: int
    queue_depth: int
    rate_limit_per_hour: int
    budget_usd: Optional[float] = None
    auto_pause_failures: Optional[int] = None
    auto_pause_idle_days: Optional[int] = None
    health: Health
    # The preflight's carry-over between runs (R10). Not a secret: the owner edits it on the detail page.
    state_document: Optional[dict[str, Any]] = None
    paused_at: Optional[str] = None
    paused_by: Optional[ActorRef] = None
    paused_reason: Optional[str] = None
    failed_at: Optional[str] = None
    failed_reason: Optional[str] = None
    consecutive_failures: int
    last_delivery_at: Optional[str] = None
    last_success_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: ActorRef
    updated_by: ActorRef
    created_by_task_id: Optional[int] = None


class SecretOut(BaseModel):
    secret: str


class TestRenderIn(StrictModel):
    # A sample event: `{"body": {...}, "headers": {...}, "query": {...}}` (or `email`/`slack`/`item`).
    sample: dict[str, Any] = Field(default_factory=dict)
    # The editor's UNSAVED templates, so Test renders what the owner is looking at. Omitted → the saved ones.
    # Validated exactly as a save would validate them; nothing is stored.
    prompt_template: Optional[str] = Field(default=None, max_length=MAX_TEMPLATE_CHARS)
    title_template: Optional[str] = Field(default=None, max_length=MAX_TITLE_TEMPLATE_CHARS)


class TestRenderOut(BaseModel):
    title: str
    body: str
    missing_paths: list[str]


class TaskRef(BaseModel):
    id: int
    title: Optional[str] = None


class DeliveryOut(BaseModel):
    id: int
    received_at: Optional[str] = None
    processed_at: Optional[str] = None
    state: str
    label: str
    reason: Optional[str] = None
    item_id: Optional[str] = None
    parent_delivery_id: Optional[int] = None
    task: Optional[TaskRef] = None
    missing_paths: list[str]
    preview: Optional[dict[str, Any]] = None
    attempts: int


class OutcomeOption(BaseModel):
    value: str
    label: str


class DeliveriesPage(BaseModel):
    items: list[DeliveryOut]
    # The outcomes this log may be filtered by, in display order, with their words — the console's filter
    # chips, so the labels are never spelled twice (`app/triggers.py::OUTCOME_LABELS`).
    outcomes: list[OutcomeOption] = Field(default_factory=list)
    # Pass as `before` for the next (older) page; None on the last.
    next_before: Optional[int] = None


class DedupOut(BaseModel):
    dedup_key: str
    first_seen_at: Optional[str] = None
    task: Optional[TaskRef] = None
    delivery_id: Optional[int] = None


class DedupPage(BaseModel):
    items: list[DedupOut]
    total: int


class UnmatchedOut(BaseModel):
    id: int
    kind: str
    received_at: Optional[str] = None
    reason: Optional[str] = None
    preview: Optional[dict[str, Any]] = None
    # An email's sender and subject, read off its stored message; None for a Slack mention or a deleted email.
    sender: Optional[str] = None
    subject: Optional[str] = None
    # Why an email was refused before routing (`inbound_emails.rejected_reason`); None for one no trigger matched.
    rejected_reason: Optional[str] = None


# ── Reads ─────────────────────────────────────────────────────────────────────────────────────────────


def _require_trigger(session: Session, trigger_id: UUID, *, for_update: bool = False) -> TaskTrigger:
    """The row, or a 404. A filtered select, so another org's trigger is one absence with a missing one."""
    return get_or_404(session, TaskTrigger, trigger_id, detail=f"no trigger with id {trigger_id}", lock=for_update)


def _url_for(trigger: TaskTrigger) -> Optional[str]:
    base = _hooks_base_url()
    if trigger.source_kind not in HTTP_SOURCE_KINDS or not trigger.ingress_token or not base:
        return None
    return f"{base}/api/public/triggers/{trigger.ingress_token}"


def _out(trigger: TaskTrigger, *, names: dict[UUID, str], projects: dict[int, str],
         refs: dict[UUID, OrgRef]) -> TriggerOut:
    state, reason = trigger_health.trigger_health(
        trigger, paused_by_name=names.get(trigger.paused_by_id) if trigger.paused_by_id else None
    )
    return TriggerOut(
        id=trigger.id,
        name=trigger.name,
        slug=trigger.slug,
        description=trigger.description,
        org=org_ref_of(refs, trigger.org_id),
        project_id=trigger.project_id,
        project_name=projects.get(trigger.project_id),
        source_kind=trigger.source_kind,
        source_config=trigger.source_config or {},
        url=_url_for(trigger),
        has_secret=trigger.secret_sealed is not None,
        secret_revealed=trigger.secret_revealed_at is not None,
        persona=trigger.persona,
        title_template=trigger.title_template,
        prompt_template=trigger.prompt_template,
        model=trigger.model,
        full_auto=trigger.full_auto,
        hidden=trigger.hidden,
        preflight_kind=trigger.preflight_kind,
        preflight_script_path=trigger.preflight_script_path,
        preflight_prompt=trigger.preflight_prompt,
        preflight_credential_id=trigger.preflight_credential_id,
        preflight_timeout_seconds=trigger.preflight_timeout_seconds,
        concurrency_cap=trigger.concurrency_cap,
        queue_depth=trigger.queue_depth,
        rate_limit_per_hour=trigger.rate_limit_per_hour,
        budget_usd=float(trigger.budget_usd) if trigger.budget_usd is not None else None,
        auto_pause_failures=trigger.auto_pause_failures,
        auto_pause_idle_days=trigger.auto_pause_idle_days,
        health=Health(state=state, reason=reason),
        paused_at=iso_or_none(trigger.paused_at),
        paused_by=(
            ActorRef(id=trigger.paused_by_id, name=names.get(trigger.paused_by_id))
            if trigger.paused_by_id else None
        ),
        paused_reason=trigger.paused_reason,
        failed_at=iso_or_none(trigger.failed_at),
        failed_reason=trigger.failed_reason,
        consecutive_failures=trigger.consecutive_failures,
        last_delivery_at=iso_or_none(trigger.last_delivery_at),
        last_success_at=iso_or_none(trigger.last_success_at),
        created_at=iso_or_none(trigger.created_at),
        updated_at=iso_or_none(trigger.updated_at),
        created_by=ActorRef(id=trigger.created_by_id, name=names.get(trigger.created_by_id)),
        updated_by=ActorRef(id=trigger.updated_by_id, name=names.get(trigger.updated_by_id)),
        created_by_task_id=trigger.created_by_task_id,
    )


def _one_out(session: Session, trigger: TaskTrigger) -> TriggerOut:
    """`last_delivery_at` is the newest delivery row's, read here rather than written by the ingest (which must never
    wait on the processor's lock on the trigger)."""
    names = actor_names(session, [trigger.created_by_id, trigger.updated_by_id, trigger.paused_by_id])
    projects = {trigger.project_id: session.scalar(select(Project.name).where(Project.id == trigger.project_id))}
    out = _out(trigger, names=names, projects=projects, refs=org_refs(session, [trigger.org_id]))
    latest = session.scalar(
        select(func.max(TriggerDelivery.received_at)).where(TriggerDelivery.trigger_id == trigger.id)
    )
    if latest is not None:
        out.last_delivery_at = iso_or_none(latest)
    out.state_document = trigger.state_document or {}
    return out


# ── Validation ────────────────────────────────────────────────────────────────────────────────────────


def _require_slack_on_plan(session: Session, trigger: TaskTrigger) -> None:
    """A Slack trigger only on a plan with Slack (`plan_terms.SLACK`): 403 naming the plan feature otherwise."""
    if trigger.source_kind == SOURCE_SLACK and trigger.org_id is not None:
        plan_terms.require_switch(session, trigger.org_id, plan_terms.SLACK)


def _validate(trigger: TaskTrigger) -> None:
    """The merged row, whole. Mirrors the table's CHECKs as readable 422s and adds what SQL cannot say."""
    trigger.name = (trigger.name or "").strip()
    if not trigger.name:
        _reject("name", "must not be blank")
    if trigger.source_kind not in SOURCE_KINDS:
        _reject("source_kind", f"must be one of {', '.join(SOURCE_KINDS)}")
    if trigger.persona not in TRIGGER_PERSONAS:
        _reject("persona", f"must be one of {', '.join(TRIGGER_PERSONAS)}")
    try:
        trigger.source_config = validate_source_config(trigger.source_kind, trigger.source_config)
    except SourceConfigError as exc:
        _reject("source_config", str(exc))
    if not (trigger.prompt_template or "").strip():
        _reject("prompt_template", "must not be blank — it becomes the filed Task's body")
    try:
        validate_template(trigger.prompt_template, source_kind=trigger.source_kind, field_name="prompt_template")
        if trigger.title_template:
            validate_template(trigger.title_template, source_kind=trigger.source_kind, field_name="title_template")
    except TemplateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        trigger.model = validate_model(trigger.model) or None
    except ValueError as exc:
        _reject("model", str(exc))
    if trigger.preflight_kind not in PREFLIGHT_KINDS:
        _reject("preflight_kind", f"must be one of {', '.join(PREFLIGHT_KINDS)}")
    if trigger.preflight_kind == PREFLIGHT_SCRIPT:
        path = (trigger.preflight_script_path or "").strip()
        if not path:
            _reject("preflight_script_path", "a script preflight needs the repo-relative path of its script")
        if path.startswith("/") or ".." in path.split("/") or "\\" in path:
            _reject("preflight_script_path", "must be a path inside the repository, with no '..' and no leading '/'")
        trigger.preflight_script_path = path
    if trigger.preflight_kind == PREFLIGHT_MODEL and not (trigger.preflight_prompt or "").strip():
        _reject("preflight_prompt", "a model preflight needs a prompt saying what to decide")
    if not MIN_PREFLIGHT_TIMEOUT_SECONDS <= trigger.preflight_timeout_seconds <= MAX_PREFLIGHT_TIMEOUT_SECONDS:
        _reject(
            "preflight_timeout_seconds",
            f"must be between {MIN_PREFLIGHT_TIMEOUT_SECONDS} and {MAX_PREFLIGHT_TIMEOUT_SECONDS} seconds",
        )
    if not 1 <= trigger.concurrency_cap <= MAX_CONCURRENCY_CAP:
        _reject("concurrency_cap", f"must be between 1 and {MAX_CONCURRENCY_CAP}")
    if not 0 <= trigger.queue_depth <= MAX_QUEUE_DEPTH:
        _reject("queue_depth", f"must be between 0 and {MAX_QUEUE_DEPTH}")
    if not 1 <= trigger.rate_limit_per_hour <= MAX_RATE_LIMIT_PER_HOUR:
        _reject("rate_limit_per_hour", f"must be between 1 and {MAX_RATE_LIMIT_PER_HOUR}")
    if trigger.budget_usd is not None and not 0 <= trigger.budget_usd <= MAX_BUDGET_USD:
        _reject("budget_usd", f"must be between 0 and {MAX_BUDGET_USD}")
    if trigger.auto_pause_failures is not None and trigger.auto_pause_failures < 1:
        _reject("auto_pause_failures", "must be at least 1, or null for off")
    if trigger.auto_pause_idle_days is not None and trigger.auto_pause_idle_days < 1:
        _reject("auto_pause_idle_days", "must be at least 1, or null for off")


def _credential_or_refuse(session: Session, trigger: TaskTrigger, owner: Optional[AdminUser]) -> None:
    """A model preflight needs a key (AD-I4): its own, else the platform's. Its own must be one this caller's
    scope reads, in the trigger's own org, of the model kind, and one the trigger's `owner` may use on its
    project — the run re-asks the same question (`llm_client.trigger_preflight_llm`). Read through the seam, so
    another org's id is one answer with a missing one."""
    if trigger.preflight_credential_id is None:
        if trigger.preflight_kind == PREFLIGHT_MODEL and not settings_reads.get_str(
            llm_client.TRIGGER_PREFLIGHT_KNOB
        ).strip():
            _reject("preflight_credential_id", "a model preflight needs an Anthropic API key — this control plane "
                    "has no platform key to fall back to")
        return
    credential = session.scalar(select(Credential).where(Credential.id == trigger.preflight_credential_id))
    if credential is None or credential.org_id != trigger.org_id:
        _reject("preflight_credential_id", f"no credential with id {trigger.preflight_credential_id}")
    if credential.type != _PREFLIGHT_CREDENTIAL_TYPE:
        _reject("preflight_credential_id", "a model preflight runs on an Anthropic API key credential")
    credential_consumers.refuse_if_nodevault(credential, credential_consumers.TRIGGER_PREFLIGHT)
    if trigger.preflight_kind != PREFLIGHT_MODEL:
        # A key left on a trigger whose preflight is off is never called; turning it off is never refused.
        return
    usable = owner is not None and session.scalar(
        select(Credential.id).where(
            Credential.id == credential.id,
            usable_credential_clause(principal_for_user(session, owner), trigger.project_id),
        )
    )
    if not usable:
        _reject("preflight_credential_id", "the trigger's owner may not use this credential on its project")


def _owner_in_org_or_refuse(owner: AdminUser, org_id: UUID) -> None:
    """A trigger files as its owner, with the owner's licence and git access; an owner in another org would file
    across the tenancy boundary (the cron rule, `automation_fire._owner_or_reason`)."""
    if owner.org_id is not None and owner.org_id != org_id:
        raise HTTPException(
            status_code=409,
            detail="project_id: a trigger files as its creator, so create it as a member of the project's "
                   "organization",
        )


def _slack_channels(trigger: TaskTrigger) -> list[str]:
    config = trigger.source_config if trigger.source_kind == SOURCE_SLACK else None
    return list(config.get("channel_ids") or ()) if isinstance(config, dict) else []


def _slack_channels_free_or_refuse(session: Session, trigger: TaskTrigger, held: Iterable[str] = ()) -> None:
    """A Slack mention routes to a trigger by channel, fleet-wide (`trigger_intake._slack_trigger_for`), so a channel
    another org's trigger already holds cannot be claimed: it would take that org's mentions (409). Only the channels
    this save adds (`held`: the ones the trigger already had) are judged. A per-channel lock, held to this
    transaction's commit, makes two orgs' simultaneous claims take turns."""
    added = sorted(set(_slack_channels(trigger)) - set(held))
    for channel in added:
        session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"slack-channel:{channel}"))))
    channel = slack_channel_held_by_another_org(added, trigger.org_id)
    if channel is not None:
        raise HTTPException(
            status_code=409,
            detail=f"source_config: Channel {channel} is already used by a trigger in another organization.",
        )


def _github_repository(trigger: TaskTrigger) -> Optional[str]:
    config = trigger.source_config if trigger.source_kind == SOURCE_GITHUB else None
    value = config.get("repository") if isinstance(config, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _connected_github_repositories(session: Session, project: Project) -> list[str]:
    """The `owner/name` of every connected github.com repository, primary first — a parse of our own rows, never a
    GitHub read. `projects.repo_url` is the primary row's projection (`app/project_repos.py`), so the rows are the
    whole answer."""
    rows = sorted(project_repos.repos_of(session, project.id), key=lambda row: not row.is_primary)
    urls = [row.repo_url for row in rows]
    paths = [path for path in (forges.github_repo_path(url) for url in urls) if path]
    return list(dict.fromkeys(paths))


def _default_github_repository(session: Session, trigger: TaskTrigger, project: Project) -> None:
    """A new GitHub trigger that does not name a `repository` listens to its project's primary repo (architecture
    §4). Set at create only, so it shows in the editor. An explicit blank (`""`, the editor's "Any repository") is
    the owner's choice and stays blank: any repository."""
    config = trigger.source_config
    if trigger.source_kind != SOURCE_GITHUB or not isinstance(config, dict) or config.get("repository") is not None:
        # A set value, valid or not, is the owner's: `_validate` and `_repository_connected_or_refuse` judge it.
        return
    connected = _connected_github_repositories(session, project)
    if connected:
        trigger.source_config = {**config, "repository": connected[0]}


def _repository_connected_or_refuse(session: Session, trigger: TaskTrigger, project: Project) -> None:
    """A GitHub trigger's `repository` names one of its project's connected repos (D9, AC7.1); blank is any. Matched
    case-insensitively, as intake matches a delivery (`trigger_intake`)."""
    repository = _github_repository(trigger)
    if repository is None:
        return
    connected = _connected_github_repositories(session, project)
    if repository.lower() in {path.lower() for path in connected}:
        return
    choices = f"one of {', '.join(connected)}" if connected else "none (it has no connected GitHub repository)"
    _reject("source_config", f"repository: {repository} is not connected to this project — connected: {choices}; "
                             "leave it blank for any repository")


def _source_allowed_or_refuse(source_kind: str, project: Project, source_config: Any = None) -> None:
    """The per-kind placement rules: an HTTP kind needs a public hooks address (409); a `user_registered` trigger
    watching platform sign-ups is the operator org's only, because those belong to no tenant yet — its
    `org` scope is every org's. Where an email trigger listens is `_email_inbox_or_refuse`'s."""
    if source_kind in HTTP_SOURCE_KINDS and not _hooks_base_url():
        raise HTTPException(status_code=409, detail=NO_HOOKS_ADDRESS)
    if (source_kind == SOURCE_USER_REGISTERED and user_scope_of(source_config) == USER_SCOPE_PLATFORM
            and project.org_id != ORG_ZERO_ID):
        raise HTTPException(
            status_code=403,
            detail="source_config: scope: only the operator's organization can watch platform sign-ups",
        )


def _email_inbox_or_refuse(session: Session, trigger: TaskTrigger) -> None:
    """Where an email trigger listens (seam S8), judged on the normalized config. A hosted address (`address_id`) or a
    Gmail connection (`inbox`) must be the trigger's own org's and needs the Tenant email plan switch (403, AS1); the
    connection must be able to receive — one connected with Google sign-in only sends, so it is no source (422).
    `system`, the deployment's own mail domain, is the operator org's alone (AD-I2)."""
    if trigger.source_kind != SOURCE_GMAIL:
        return
    if not edition_features.enabled("tenant_mail"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import tenant_gmail
    from app.tenant_gmail import addresses as tenant_addresses

    config = trigger.source_config or {}
    address_id, inbox = config.get("address_id"), str(config.get("inbox") or trigger_intake.SYSTEM_INBOX)
    if address_id:
        plan_terms.require_switch(session, trigger.org_id, plan_terms.TENANT_EMAIL)
        if tenant_addresses.address(session, trigger.org_id, UUID(str(address_id))) is None:
            _reject("source_config", "address_id: no such address in this organization — create one in Settings › "
                                     "Organizations › Email › Addresses")
        return
    if inbox == trigger_intake.SYSTEM_INBOX:
        if trigger.org_id != ORG_ZERO_ID:
            _reject("source_config", "inbox: choose this organization's hosted address (address_id) or its "
                                     "service-account Gmail connection")
        return
    plan_terms.require_switch(session, trigger.org_id, plan_terms.TENANT_EMAIL)
    connection = tenant_gmail.connection(session, trigger.org_id, UUID(inbox))
    if connection is None:
        _reject("source_config", "inbox: no such Gmail connection in this organization")
    elif not connection.can_receive:
        _reject("source_config", f"inbox: {connection.address} is connected with Google sign-in, which only sends — "
                                 "connect it with a service account to receive")


def _bind_email_address(session: Session, trigger: TaskTrigger) -> None:
    """The hosted address the trigger names feeds it alone (`tenant_gmail.addresses.bind`); 422 when another
    trigger holds it. Flushes first, so the address's key finds the trigger row."""
    if trigger.source_kind != SOURCE_GMAIL:
        return
    if not edition_features.enabled("tenant_mail"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    from app import tenant_gmail
    from app.tenant_gmail import addresses as tenant_addresses

    address_id = (trigger.source_config or {}).get("address_id")
    session.flush()
    try:
        tenant_addresses.bind(session, trigger.org_id, trigger.id, UUID(str(address_id)) if address_id else None)
    except tenant_gmail.TenantEmailError as exc:
        _reject("source_config", str(exc))


# ── Routes and writers ────────────────────────────────────────────────────────────────────────────────


@router.get("/unmatched", response_model=list[UnmatchedOut])
@org_admin(Tenancy.CALLER_PLANE)
def list_unmatched(
    kind: Optional[str] = Query(default=None, pattern="^(gmail|slack)$"),
    limit: int = Query(default=DELIVERIES_DEFAULT_LIMIT, ge=1, le=DELIVERIES_MAX_LIMIT),
    actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[UnmatchedOut]:
    """Mail and Slack mentions no trigger matched, in the caller's OWN org only. The operator's are org zero's: the
    deployment's mail domain and Slack, both deployment-global. An org admin's are the mail its hosted addresses
    and Gmail connections received; never another org's, the operator's included (Slack mentions are
    always org zero's)."""
    # The caller's own org: the operator's is org zero.
    statement = select(TriggerDelivery).where(
        TriggerDelivery.trigger_id.is_(None), TriggerDelivery.state == UNMATCHED, TriggerDelivery.org_id == actor.org_id,
    )
    if kind == SOURCE_GMAIL:
        statement = statement.where(TriggerDelivery.inbound_email_id.is_not(None))
    elif kind == SOURCE_SLACK:
        statement = statement.where(TriggerDelivery.slack_event_id.is_not(None))
    rows = session.scalars(statement.order_by(TriggerDelivery.id.desc()).limit(limit)).all()
    email_ids = {row.inbound_email_id for row in rows if row.inbound_email_id is not None}
    emails = {
        email.id: email
        for email in (session.scalars(select(InboundEmail).where(InboundEmail.id.in_(email_ids))) if email_ids else ())
    }
    out = []
    for row in rows:
        email = emails.get(row.inbound_email_id) if row.inbound_email_id is not None else None
        out.append(UnmatchedOut(
            id=row.id,
            kind=SOURCE_GMAIL if row.inbound_email_id is not None else SOURCE_SLACK,
            received_at=iso_or_none(row.received_at),
            reason=row.reason,
            preview=row.preview,
            sender=email.from_address if email is not None else None,
            subject=email.subject if email is not None else None,
            rejected_reason=email.rejected_reason if email is not None else None,
        ))
    return out


def write_trigger_create(
    session: Session, body: TriggerCreate, actor: AdminUser, *, created_by_task_id: Optional[int] = None,
    automation: Optional["automation_service.ParentSpec"] = None, paused: bool = False,
) -> tuple[TaskTrigger, Optional[str]]:
    """The create writer of `/api/automations` and the agent surface (`routes/agent_triggers.py`), so every create
    meets the same refusals. Returns the committed row and the secret to answer, if
    any. The row's parent Automation is created in the same transaction (`automation_service.sync_trigger`).

    A console create answers the secret, so its one reveal is spent here. An agent create (`created_by_task_id`)
    never answers it: the reveal stays owed, for a human on the detail page (PRD R19)."""
    # Another org's project reads as absent, so naming it is a 404 (AC-2.4).
    project = project_or_404(session, body.project_id, _no_such_project(body.project_id))
    if body.source_kind not in SOURCE_KINDS:
        _reject("source_kind", f"must be one of {', '.join(SOURCE_KINDS)}")
    # An email trigger listens on the org's connected Gmail or hosted address: the edition's own refusal first.
    if body.source_kind == SOURCE_GMAIL and not edition_features.enabled("tenant_mail"):
        raise HTTPException(status_code=404, detail=ce_caps.NOT_IN_CE)
    if edition.IS_CE:
        raise HTTPException(status_code=409, detail=NO_EVENTS_LOCALLY)
    _owner_in_org_or_refuse(actor, project.org_id)
    trigger = TaskTrigger(
        id=new_uuid7(),
        org_id=project.org_id,
        project_id=project.id,
        **body.model_dump(exclude={"project_id"}),
    )
    _default_github_repository(session, trigger, project)
    _validate(trigger)
    _require_slack_on_plan(session, trigger)
    _repository_connected_or_refuse(session, trigger, project)
    # After `_validate`: the placement rule reads the NORMALIZED config, never the raw body.
    _source_allowed_or_refuse(trigger.source_kind, project, trigger.source_config)
    _email_inbox_or_refuse(session, trigger)
    _slack_channels_free_or_refuse(session, trigger)
    _credential_or_refuse(session, trigger, actor)
    if trigger.source_kind == SOURCE_WEBHOOK and trigger_secrets.webhook_auth(trigger.source_config) != AUTH_API_KEY:
        _reject("source_config", LEGACY_AUTH_REFUSED)
    secret: Optional[str] = None
    if trigger.source_kind in HTTP_SOURCE_KINDS:
        trigger.ingress_token = trigger_secrets.new_ingress_token()
    if trigger_secrets.takes_secret(trigger.source_kind, trigger.source_config):
        secret = trigger_secrets.new_secret()
        trigger.secret_sealed = trigger_secrets.seal_secret(trigger.id, secret)
        if created_by_task_id is None:
            # Answered in this response: the one reveal happens now.
            trigger.secret_revealed_at = datetime.now(timezone.utc)
        else:
            secret = None
    stamp_actor(trigger, actor, created=True)
    trigger.created_by_task_id = created_by_task_id
    if paused:
        # Born paused (an automation saved paused), in the create's own commit: no delivery can file in between.
        trigger.paused_at = datetime.now(timezone.utc)
        trigger.paused_by_id = actor.id
    add_with_org_slug(session, trigger, trigger.name, default="trigger")
    _bind_email_address(session, trigger)
    automation_service.sync_trigger(session, trigger, automation)
    session.commit()
    session.refresh(trigger)
    return trigger, secret


def write_trigger_update(
    session: Session, trigger: TaskTrigger, body: TriggerUpdate, actor: AdminUser, *,
    via_task_id: Optional[int] = None, automation: Optional["automation_service.ParentSpec"] = None,
) -> TaskTrigger:
    """The update writer of `/api/automations` and the agent surface (`routes/agent_triggers.py`). `trigger` must
    already be row-locked; `via_task_id` names the runner's task in the audit row. The parent Automation is kept in
    step before the commit (`automation_service.sync_trigger`)."""
    provided = body.model_fields_set
    if "source_kind" in provided and body.source_kind != trigger.source_kind:
        _reject("source_kind", "cannot change after create — create a new trigger for another source")
    held_auth = trigger_secrets.webhook_auth(trigger.source_config)
    changed: list[str] = []
    held_channels = _slack_channels(trigger)
    held_repository = (_github_repository(trigger) or "").lower()
    with session.no_autoflush:
        if "project_id" in provided and body.project_id != trigger.project_id:
            if body.project_id is None:
                _reject("project_id", "must name a project")
            project = project_or_404(session, body.project_id, _no_such_project(body.project_id))
            if project.org_id != trigger.org_id:
                # The seam already hides another org's project from a tenant; the operator reads every org, and a
                # trigger never leaves its own.
                raise HTTPException(status_code=404, detail=_no_such_project(body.project_id))
        for field in provided - {"source_kind"}:
            value = getattr(body, field)
            if value is None and field not in _NULLABLE_FIELDS:
                _reject(field, "cannot be null")
            if getattr(trigger, field) != value:
                setattr(trigger, field, value)
                changed.append(field)
        if trigger.source_kind == SOURCE_WEBHOOK:
            _keep_or_switch_auth(trigger, held_auth, body.source_config if "source_config" in provided else None)
        _validate(trigger)
        _require_slack_on_plan(session, trigger)
        if (_github_repository(trigger) or "").lower() != held_repository:
            # Judged only when the repository itself moves (`held_repository` is the stored, already-normalized
            # value). An older trigger naming a repo that is not connected keeps saving its other fields and keeps
            # listening (intake is unchanged); so does one whose project moved, since an automation re-derives its
            # child's project on every save (`automation_service._carrier_project`) and a rename must not fail.
            _repository_connected_or_refuse(
                session, trigger, project_or_404(session, trigger.project_id, _no_such_project(trigger.project_id))
            )
        if "source_config" in changed:
            _slack_channels_free_or_refuse(session, trigger, held_channels)
            _email_inbox_or_refuse(session, trigger)
            _bind_email_address(session, trigger)
            if trigger.source_kind == SOURCE_USER_REGISTERED:
                # The row has no `project` relationship: the placement rule reads the project's org.
                project = project_or_404(
                    session, trigger.project_id, _no_such_project(trigger.project_id)
                )
                _source_allowed_or_refuse(trigger.source_kind, project, trigger.source_config)
        if {"preflight_kind", "preflight_credential_id", "project_id"} & set(changed):
            _credential_or_refuse(session, trigger, trigger_owner(session, trigger))
    if not changed:
        return trigger
    stamp_actor(trigger, actor)
    record_write(
        session, actor, "trigger_update", f"Updated trigger {trigger.name} ({', '.join(sorted(changed))})",
        data={"trigger_id": str(trigger.id), "changed": sorted(changed), **_via(via_task_id)},
    )
    session.flush()
    automation_service.sync_trigger(session, trigger, automation)
    session.commit()
    session.refresh(trigger)
    return trigger


def _keep_or_switch_auth(trigger: TaskTrigger, held: str, sent: Optional[dict[str, Any]]) -> None:
    """A webhook edit's scheme, IN PLACE: one way only, from a legacy secret to `api_key`, which drops the secret.
    An edit not naming `auth` keeps the stored one (a legacy trigger never switches by omission)."""
    if not isinstance(sent, dict) or "auth" not in sent:
        trigger.source_config = {**(trigger.source_config or {}), "auth": held}
        return
    wanted = str(sent.get("auth") or "")
    if wanted == held:
        return
    if wanted != AUTH_API_KEY:
        _reject("source_config", LEGACY_AUTH_REFUSED)
    trigger.secret_sealed = None
    trigger.secret_revealed_at = None


def write_trigger_delete(
    session: Session, trigger: TaskTrigger, actor: AdminUser, *, via_task_id: Optional[int] = None
) -> None:
    """The delete writer of `/api/automations` and the agent surface (`routes/agent_triggers.py`); `via_task_id` as
    in `write_trigger_update`."""
    record_write(
        session, actor, "trigger_delete", f"Deleted trigger {trigger.name}",
        data={"trigger_id": str(trigger.id), "source_kind": trigger.source_kind, **_via(via_task_id)},
    )
    # The parent automation goes with its last child (revision 0417), its Task-action files' rows with it; their
    # bytes are reaped after the commit, as a task delete's are.
    keys = automation_service.attachment_keys(session, trigger.automation_id)
    session.delete(trigger)
    session.commit()
    automation_service.reap_attachment_keys(session, keys)


def reveal_trigger_secret(session: Session, trigger_id: UUID, response: Response) -> SecretOut:
    """The secret, once, while its one reveal is still owed (a trigger an advisor created); 409 after."""
    trigger = _require_trigger(session, trigger_id, for_update=True)
    if trigger.secret_sealed is None:
        raise HTTPException(status_code=409, detail=NO_SECRET)
    if trigger.secret_revealed_at is not None:
        raise HTTPException(status_code=409, detail=SECRET_ALREADY_SHOWN)
    secret = trigger_secrets.open_secret(trigger)
    trigger.secret_revealed_at = datetime.now(timezone.utc)
    session.commit()
    response.headers.update(_NO_STORE)
    return SecretOut(secret=secret)


def rotate_trigger_secret(session: Session, trigger_id: UUID, response: Response, actor: AdminUser) -> SecretOut:
    """A new secret, answered once; the old one stops verifying at once."""
    trigger = _require_trigger(session, trigger_id, for_update=True)
    if not trigger_secrets.takes_secret(trigger.source_kind, trigger.source_config):
        raise HTTPException(status_code=409, detail=NO_SECRET)
    secret = trigger_secrets.new_secret()
    trigger.secret_sealed = trigger_secrets.seal_secret(trigger.id, secret)
    trigger.secret_revealed_at = datetime.now(timezone.utc)
    stamp_actor(trigger, actor)
    session.commit()
    response.headers.update(_NO_STORE)
    return SecretOut(secret=secret)


def write_pause(session: Session, trigger: TaskTrigger, actor: AdminUser, reason: str) -> None:
    """`/api/automations`' pause (an automation's `paused` is its child's). `trigger` must already be row-locked.
    Commits."""
    trigger.paused_at = datetime.now(timezone.utc)
    trigger.paused_by_id = actor.id
    trigger.paused_reason = reason.strip() or None
    stamp_actor(trigger, actor)
    session.commit()
    session.refresh(trigger)


def write_resume(session: Session, trigger: TaskTrigger, actor: AdminUser) -> None:
    """`/api/automations`' resume: clears a human pause and a system failure, and resets the failure streak
    (AC-12.2). `trigger` must already be row-locked. Commits."""
    trigger_health.clear_stops(trigger)
    stamp_actor(trigger, actor)
    session.commit()
    session.refresh(trigger)


def test_render_trigger(session: Session, trigger_id: UUID, body: TestRenderIn) -> TestRenderOut:
    """Render the templates over a pasted sample event — the editor's unsaved ones when sent, else the saved
    ones. Files nothing and stores nothing (AC-4.1)."""
    trigger = _require_trigger(session, trigger_id)
    prompt_template = trigger.prompt_template if body.prompt_template is None else body.prompt_template
    title_template = trigger.title_template if body.title_template is None else body.title_template
    try:
        validate_template(prompt_template, source_kind=trigger.source_kind, field_name="prompt_template")
        if title_template:
            validate_template(title_template, source_kind=trigger.source_kind, field_name="title_template")
    except TemplateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    event = dict(body.sample)
    if isinstance(event.get("headers"), dict):
        event["headers"] = redact_headers(event["headers"])
    rendered = render(prompt_template, event)
    missing = list(rendered.missing_paths)
    title = ""
    if title_template:
        rendered_title = render_title(title_template, event)
        title = rendered_title.text
        missing += [p for p in rendered_title.missing_paths if p not in missing]
    return TestRenderOut(title=title, body=rendered.text, missing_paths=missing)


def _task_refs(session: Session, task_ids: set[int]) -> dict[int, TaskRef]:
    if not task_ids:
        return {}
    rows = session.execute(select(Task.id, Task.title).where(Task.id.in_(task_ids))).all()
    return {row.id: TaskRef(id=row.id, title=row.title) for row in rows}


def list_trigger_deliveries(
    session: Session, trigger_id: UUID, outcome: list[str], before: Optional[int], limit: int,
) -> DeliveriesPage:
    """The deliveries log, newest first, filterable by outcome (repeat `outcome`), paged by `before`."""
    _require_trigger(session, trigger_id)
    unknown = [value for value in outcome if value not in DELIVERY_STATES]
    if unknown:
        _reject("outcome", f"unknown outcome {unknown[0]!r}; expected one of {', '.join(DELIVERY_STATES)}")
    statement = select(TriggerDelivery).where(TriggerDelivery.trigger_id == trigger_id)
    if outcome:
        statement = statement.where(TriggerDelivery.state.in_(outcome))
    if before is not None:
        statement = statement.where(TriggerDelivery.id < before)
    rows = session.scalars(statement.order_by(TriggerDelivery.id.desc()).limit(limit + 1)).all()
    page, more = rows[:limit], len(rows) > limit
    tasks = _task_refs(session, {row.task_id for row in page if row.task_id is not None})
    # A task this caller cannot read still names its number: the delivery says what was filed.
    return DeliveriesPage(
        items=[
            DeliveryOut(
                id=row.id,
                received_at=iso_or_none(row.received_at),
                processed_at=iso_or_none(row.processed_at),
                state=row.state,
                label=OUTCOME_LABELS.get(row.state, row.state),
                reason=row.reason,
                item_id=row.item_id,
                parent_delivery_id=row.parent_delivery_id,
                task=tasks.get(row.task_id) or (TaskRef(id=row.task_id) if row.task_id is not None else None),
                missing_paths=list(row.missing_paths or []),
                preview=row.preview,
                attempts=row.attempts,
            )
            for row in page
        ],
        next_before=page[-1].id if more and page else None,
        # `unmatched` belongs to no trigger, so a trigger's own log never holds one.
        outcomes=[
            OutcomeOption(value=value, label=OUTCOME_LABELS[value]) for value in OUTCOMES if value != UNMATCHED
        ],
    )


def list_trigger_dedup(session: Session, trigger_id: UUID, limit: int, offset: int) -> DedupPage:
    """The items this trigger has acted on, newest first; each holds its key for 90 days unless cleared."""
    _require_trigger(session, trigger_id)
    where = TriggerDedup.trigger_id == trigger_id
    total = session.scalar(select(func.count()).select_from(TriggerDedup).where(where)) or 0
    rows = session.scalars(
        select(TriggerDedup).where(where).order_by(TriggerDedup.first_seen_at.desc()).offset(offset).limit(limit)
    ).all()
    tasks = _task_refs(session, {row.task_id for row in rows if row.task_id is not None})
    return DedupPage(
        items=[
            DedupOut(
                dedup_key=row.dedup_key,
                first_seen_at=iso_or_none(row.first_seen_at),
                task=tasks.get(row.task_id) or (TaskRef(id=row.task_id) if row.task_id is not None else None),
                delivery_id=row.delivery_id,
            )
            for row in rows
        ],
        total=total,
    )


def clear_trigger_dedup(session: Session, trigger_id: UUID, dedup_key: str, actor: AdminUser) -> Response:
    """Clear one entry, so the same item may file again (AC-9.2)."""
    trigger = _require_trigger(session, trigger_id)
    removed = session.execute(
        delete(TriggerDedup).where(TriggerDedup.trigger_id == trigger_id, TriggerDedup.dedup_key == dedup_key)
    ).rowcount
    if not removed:
        raise HTTPException(status_code=404, detail=f"no dedup entry {dedup_key!r} on this trigger")
    record_write(
        session, actor, "trigger_dedup_clear", f"Cleared a dedup entry of trigger {trigger.name}",
        data={"trigger_id": str(trigger.id), "dedup_key": dedup_key},
    )
    session.commit()
    return Response(status_code=204)


@router.put("/{trigger_id}/state", response_model=TriggerOut)
@org_admin(Tenancy.CALLER_PLANE)
def put_trigger_state(
    trigger_id: UUID,
    document: Any = Body(...),
    actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TriggerOut:
    """Replace the state document the preflight reads and replaces (R10): a JSON object, at most 64 KiB."""
    trigger = _require_trigger(session, trigger_id, for_update=True)
    if not isinstance(document, dict):
        _reject("state", "must be a JSON object")
    size = len(json.dumps(document).encode())
    if size > MAX_STATE_BYTES:
        _reject("state", f"is {size} bytes; the state document is capped at {MAX_STATE_BYTES} bytes (64 KiB)")
    trigger.state_document = document
    stamp_actor(trigger, actor)
    session.commit()
    session.refresh(trigger)
    return _one_out(session, trigger)
