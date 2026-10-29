"""System-wide app settings — GET/PUT /api/settings.

GET returns a bare `app_title` (backward compatible, mirrored by `GET /api/public/settings`) plus the
operator-settings catalogue as `settings`. PUT is the writer: the operator's (the platform tier), or a tenant
Org Admin's for its own org's `tenant_editable` knobs (the org tier). Knobs, precedence and
typed accessors live in `app/operator_settings/`; this module is wire shape + auth.

The catalogue is narrowed per caller in `_settings_out` — this module's one authorization decision
beyond its decorators. `apps/web/src/pages/Settings.tsx` filters on the same field as presentation only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import edition_features
from app.operator_settings import (
    cache as settings_cache,
    catalog as settings_catalog,
    defaults as settings_defaults,
    errors as settings_errors,
    reads as settings_reads,
    writes as settings_writes,
)
from app.auth import require_operator, require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.app_defaults import default_app_title
from app.db import get_session
from app.entity_shape import StrictModel, conflict_as_409
from app.events import emit_console_event
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser
from app.models.orgs import AppSettings, Org
from app.operator_settings.catalog_integrations import LOCAL_RECAST
from app.tenancy.ids import is_tenant_org

# The community edition has no Telegram: `put_settings` reads this name behind the same check, and the routes
# that read it are on `telegram_router`, which only `app/routers_cloud.py` includes.
if edition_features.enabled("telegram"):
    from app import telegram_bot

log = logging.getLogger("cockpit-settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])
# Telegram's webhook registration (cloud-only, included by `app/routers_cloud.py`).
telegram_router = APIRouter(prefix="/api/settings", tags=["settings"])


def is_operator(user: AdminUser) -> bool:
    """Whether `user` is the deployment's operator, rather than a tenant's own administrator.

    `is_superadmin` alone is not the answer: a tenant org's superadmin holds it too. The verdict
    is `tenancy/ids.py::is_operator_plane` (flag and org), shared with `authz.Principal.is_operator`;
    `OPERATOR_PLANE_ORG_IDS` there decides which org counts (org zero alone).

    It authorizes configuration, never rows (`app/authz/catalog.py::Tenancy`). Not a `Depends`, to avoid resolving
    a second `AdminUser`; the PUT reaches it through `_settings_writer`.
    """
    from app.tenancy.ids import is_operator_plane

    return is_operator_plane(user.org_id, user.is_superadmin)


def app_title_for(session: Session, org: Optional[UUID]) -> str:
    """One org's `app_title`, or the code default when that org has no row.

    Never writes: `GET /api/public/settings` is unauthenticated and unbound, and a create-on-miss would
    insert an orgless row (spec §4.5, F8). A miss is an org created since the split revision or the seed
    (`app/seed.py::_seed_app_settings`) ran, answered with the product default.

    The org is passed in, not read from scope, and the WHERE clause is not redundant: the public route runs
    under `seam.operator_scope()`, where `seam.criteria_for` applies no predicate, and asks for org zero by
    name. The routes in this module are `@feature`, so they run `ORG_SCOPED` on the caller's own org.
    """
    title = session.scalar(select(AppSettings.app_title).where(AppSettings.org_id == org))
    return title if title is not None else default_app_title()


def ensure_app_settings(session: Session, org: UUID) -> bool:
    """Give `org` its branding row if it has none. `True` when this call inserted one.

    The one writer, called where an org comes into existence (`app/seed.py`), keeping one row per org by
    construction. An org created without it just shows the default title.
    """
    existing = session.scalar(select(AppSettings.id).where(AppSettings.org_id == org))
    if existing is not None:
        return False
    session.add(AppSettings(org_id=org))
    return True


class SettingKnobOut(BaseModel):
    """One catalogue entry, annotated with its live state. Mirrors `operator_settings.effective_settings`'s
    dict shape.
    """

    key: str
    label: str
    help: str
    kind: str
    # The control plane's env-var name for the knob; `_settings_out` blanks it for tenants.
    env: Optional[str] = None
    default: Any
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: Optional[str] = None
    # Which tier owns this knob — `"operator"` or `"org"` (`Knob.tier`; resolution `org > operator > env >
    # default`). `_settings_out` serves non-operators only `tier == "org"` knobs and is the boundary;
    # `pages/Settings.tsx` filters the same way for presentation. A classification, not a secret.
    tier: str
    group: str
    # Rendering fields: `section` is the card within `group`, `choices` a closed value set (select). Must
    # be declared or the model drops them.
    section: Optional[str] = None
    choices: Optional[list[str]] = None
    # Holds a `credentials.id`: the page renders a credential picker filtered to these types. Declared for
    # the same reason as above.
    credential_type: Optional[list[str]] = None
    # `choices`' display words, paired positionally (None: the page humanises each token), and whether the
    # value names an operator Multi-Node Host (the page's host picker). Declared for the same reason.
    choice_labels: Optional[list[str]] = None
    # A warning shown while that choice is selected, paired positionally ("" = none).
    choice_notes: Optional[list[str]] = None
    vnode_host: bool = False
    # An EC2 instance type from the allowlist: `choices` is that list, served live (`Knob.instance_type`).
    instance_type: bool = False
    # A `billing_plans.key`: `choices` are the active plans, served live (`Knob.plan`).
    plan: bool = False
    # A cloud connection's id: the page renders the connections select (`Knob.cloud_connection`).
    cloud_connection: bool = False
    # The `data_regions` list: the page renders its own panel instead of a row (`Knob.data_regions`).
    data_regions: bool = False
    # A tenant's own Org Admin may save this knob for its org (`Knob.tenant_editable`; `SettingsOut.org_editable`).
    tenant_editable: bool = False
    value: Any
    fallback: Any
    overridden: bool


class SettingsOut(BaseModel):
    app_title: str
    settings: list[SettingKnobOut] = []
    # Whether this caller may save — the `is_operator` verdict the PUT enforces, served so the browser need
    # not know the operator org ids (`routes/orgs.py::OrgOut.is_operator_plane` precedent). A tenant superadmin would
    # otherwise see a Save bar that 403s. Presentation only; defaults `False` (fail closed, read-only).
    editable: bool = False
    # Whether this caller may save the `tenant_editable` knobs for its own org: a tenant org's Org Admin who is not
    # an operator (`_is_tenant_org_admin`, the PUT's org-tier arm). Operators use `editable`. Fail closed.
    org_editable: bool = False


class SettingsIn(BaseModel):
    """PUT body: a partial update. Only the keys present in `values` are touched; a value of `null`
    clears that key's override (reverting it to its env-or-default fallback).
    """

    values: dict[str, Any]
    #: The operator confirmed taking the Telegram bot's webhook from another deployment
    #: (`TELEGRAM_WEBHOOK_ELSEWHERE`); without it such a save is a 409 that writes nothing.
    take_telegram_webhook: bool = False


#: The 409 `detail.code` of a Bot knob save whose bot delivers to another deployment (`telegram_bot.webhook_elsewhere`).
TELEGRAM_WEBHOOK_ELSEWHERE = "telegram_webhook_elsewhere"
#: The 422 `detail.code` of a Telegram refusal fixed in the bot's credential (`TelegramError.token_refused`): the
#: console keys its credential next step on it, never on the sentence.
TELEGRAM_TOKEN_REFUSED = "telegram_token_refused"


def _settings_out(
    session: Session, org: Optional[UUID], *, operator: bool, org_editable: bool = False
) -> SettingsOut:
    """The read model both routes here return, narrowed to what this caller may see: an operator gets every
    knob, a non-operator only the `tier == "org"` knobs a tenant may see (`Knob.tenant_visible`).

    This is the server-side tier boundary: `GET` needs only `system:view`, which org admins can hold.
    `pages/Settings.tsx` filters too, as presentation.

    It lives here, not in `app/operator_settings/`: `effective_settings` answers per knob with no
    principal; per-caller narrowing (this, and `app_title_for` on the caller's org) belongs to the wire
    layer. Both routes use it, so the PUT's response is narrowed too.

    "Operator" is not the tenancy plane (both routes bind the caller's own org via `@feature`) nor the
    `is_superadmin` flag alone; it is `is_operator`.

    Fail closed: the test is `== "org"`, never `!= "operator"`. `tests/test_settings.py` asserts no area is
    left empty; operator-only groups lose their tab for a tenant (see `pages/Settings.tsx`).
    """
    # A tenant sees org-tier knobs resolved for their org; the operator sees the unfiltered composition
    # (`effective_settings` owns what the two views mean; parity oracle in `tests/test_settings.py`).
    knobs = settings_defaults.effective_settings(session, org=None if operator else org)
    if not operator:
        # `env` names the variable the control plane's process reads — an operator fact. Blanked to `None` for
        # tenants.
        # Fail closed twice: only org-tier knobs, and never one the operator keeps to itself (`Knob.tenant_visible`).
        knobs = [
            {**knob, "env": None} for knob in knobs
            if knob["tier"] == "org" and knob["key"] not in settings_catalog.TENANT_HIDDEN_KEYS
        ]
    if not edition_features.enabled("support_assistant"):
        knobs = [{**knob, **LOCAL_RECAST.get(knob["key"], {})} for knob in knobs]
    # One `operator` decides both the narrowing and the save affordance, so they cannot disagree.
    return SettingsOut(
        app_title=app_title_for(session, org), settings=knobs, editable=operator,
        org_editable=org_editable and not operator,
    )


def _is_tenant_org_admin(user: AdminUser, principal: Optional[Principal]) -> bool:
    """A tenant org's Org Owner/Admin who is not the operator: the one caller the PUT's org-tier arm admits.

    `authz.org_admin_writer`'s rule (`Principal.is_org_admin` inside a tenant org); on the operator plane an
    org role administers nothing, so an operator-plane admin stays refused."""
    return (
        not is_operator(user)
        and is_tenant_org(user.org_id)
        and principal is not None
        and principal.is_org_admin
    )


@router.get("", response_model=SettingsOut)
@feature("system", "view")
def get_settings(
    user: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """The Settings read model for this caller. See `_settings_out`, `is_operator` and `_is_tenant_org_admin`."""
    return _settings_out(
        session, user.org_id, operator=is_operator(user),
        org_editable=_is_tenant_org_admin(user, principal),
    )


# The PUT's operator gate; an operator's write lands on `system_state`, the tier every org resolves.
# Wraps `require_user`, not `require_superadmin` (weaker, and would refuse tenant admins first in fleet
# vocabulary). An org-tier knob saved by the operator is the value every org without its own override inherits
# (one org's: `routes/orgs.py::put_org_settings`, or a tenant Org Admin's own via `_settings_writer`). 403 with two messages keyed on the plane:
# operator staff vs tenant vocabulary (`app/tenancy/copy.py`, `tenancy/ids.py::is_tenant_org`).
def _operator_actor(actor: AdminUser = Depends(require_user)) -> AdminUser:
    """The PUT's operator gate, as a dependency — so it runs before body validation, not only before the write.

    FastAPI validates a body after solving dependencies; as a handler statement, `{}` got a 422 before
    the refusal. Not `require_operator`: that hides a whole operator door (404), while this door is shared, so its
    refusal is two sentences keyed on the plane; the verdict (`is_operator`) is reused. Reached through
    `_settings_writer` alone; the operator-only settings doors take `require_operator`.
    """
    if not is_operator(actor):
        if is_tenant_org(actor.org_id):
            detail = (
                "An operator sets these for your organization — ask an operator for the change you need."
            )
        else:
            detail = (
                "Settings is fleet-wide, so only an operator may change it — ask an operator for "
                "the change you need."
            )
        raise HTTPException(status_code=403, detail=detail)
    return actor


def _settings_writer(
    actor: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
) -> AdminUser:
    """The PUT's gate: the operator, or a tenant org's Org Admin (`_is_tenant_org_admin`). Everyone else gets
    `_operator_actor`'s refusal, still before body validation. Which keys the Org Admin may send is the
    handler's check: it needs the body."""
    if _is_tenant_org_admin(actor, principal):
        return actor
    return _operator_actor(actor)


def _put_org_settings(session: Session, actor: AdminUser, values: dict[str, Any]) -> SettingsOut:
    """A tenant Org Admin's save: its own org's overrides of `tenant_editable` knobs, nothing else.

    Any other key (an unknown one included) gets the gate's tenant 403, before anything is written. `null`
    clears the override, so the org inherits the operator's value again. Conflict-handled as
    `routes/orgs.py::put_org_settings`, the operator's door to the same rows. No hand-written audit row: the
    request middleware's `put_settings` row already holds every fact (the actor, its org, the body), which is
    `app/activity_kinds.py::KEPT_RECORD_WRITE_SITES`' bar for one."""
    if not set(values) <= settings_catalog.TENANT_EDITABLE_KEYS:
        # `_operator_actor`'s tenant sentence (this caller is in a tenant org), so refusals here read alike.
        raise HTTPException(
            status_code=403,
            detail="An operator sets these for your organization — ask an operator for the change you need.",
        )
    try:
        # Two saves raced to create the same key: a 409, and nothing of this one landed.
        with conflict_as_409(session, "Changed by someone else just now. Reload and save again."):
            settings_writes.apply_org_overrides(session, actor.org_id, values)
    except settings_errors.OperatorSettingsError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    settings_cache.invalidate_cache()
    # The operator's door emits the same event, so every console showing this org refreshes either way.
    emit_console_event(session, entity="org", id=actor.org_id, action="upserted")
    session.commit()
    return _settings_out(session, actor.org_id, operator=False, org_editable=True)


@router.put("", response_model=SettingsOut)
@feature("system", "edit")
def put_settings(
    body: SettingsIn,
    # Named, because the timezone save below logs who made it.
    actor: AdminUser = Depends(_settings_writer),
    session: Session = Depends(get_session),
):
    if not is_operator(actor):
        # Past `_settings_writer`, a non-operator is a tenant Org Admin: the org tier, never `system_state`.
        return _put_org_settings(session, actor, body.values)
    # Local import: keeps the cron runtime's import graph out of this wire-shape module.
    from app import cron_reconcile, cron_runtime

    # Before the write, resolved rather than raw: only a change of effective zone counts. The invalidate is
    # needed because `overrides()` has a per-process 3s cache, so another replica could read a stale "before".
    settings_cache.invalidate_cache()
    zone_before = cron_runtime.resolved_timezone_name()
    telegram = edition_features.enabled("telegram")
    telegram_bot_before = settings_reads.get_text(telegram_bot.SETTING_KEY) if telegram else None
    telegram_bot_id = body.values.get(telegram_bot.SETTING_KEY) if telegram else None
    if isinstance(telegram_bot_id, str) and telegram_bot_id.strip() and not body.take_telegram_webhook:
        # Before the write: a bot holds one webhook, so saving it here silently takes it from the deployment it
        # points at (dev and prod designating one bot). Asked first; the console re-sends with the flag.
        elsewhere = telegram_bot.webhook_elsewhere(telegram_bot_id.strip())
        if elsewhere is not None:
            raise HTTPException(status_code=409, detail={
                "code": TELEGRAM_WEBHOOK_ELSEWHERE,
                "url": elsewhere,
                "message": f"This bot delivers elsewhere ({elsewhere}): another deployment, or this one's old "
                           "address. Saving points it here; anything there stops receiving its Telegram messages.",
            })
    try:
        # The writer's own org (operator plane, per the gate). Used only to refuse a credential pointer this
        # org cannot see.
        settings_writes.apply_overrides(session, body.values, org=actor.org_id)
    except settings_errors.OperatorSettingsError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    settings_cache.invalidate_cache()
    if isinstance(telegram_bot_id, str) and telegram_bot_id.strip():
        # After the write validated the pointer (its org and type), so a refused save never moves the bot's
        # webhook. A bot Telegram refuses is put back to the one before: the webhook is what links chats, and a
        # designation without it links nobody. Re-picking re-registers it (after a token rotation, too).
        try:
            telegram_bot.register_webhook(telegram_bot_id.strip())
        except telegram_bot.TelegramError as exc:
            settings_writes.apply_overrides(
                session, {telegram_bot.SETTING_KEY: telegram_bot_before}, org=actor.org_id
            )
            settings_cache.invalidate_cache()
            raise _telegram_refusal(exc, f"{exc} The bot was not saved.", key=telegram_bot.SETTING_KEY) from None
    if telegram:
        telegram_bot_after = settings_reads.get_text(telegram_bot.SETTING_KEY)
        if telegram_bot_before and telegram_bot_before != telegram_bot_after:
            # Cleared or replaced: the bot no longer designated stops delivering updates here.
            telegram_bot.delete_webhook(telegram_bot_before, keep=telegram_bot_after or None)
    # After the invalidate, so this reads the committed write, not a cached snapshot.
    zone_after = cron_runtime.resolved_timezone_name()
    if zone_after != zone_before:
        # The one knob with a runtime consequence: crons re-phase relative to the zone. Never raises — see
        # `cron_reconcile.apply_timezone_change`.

        # Logged with the principal: the save touches every `crons` row and can suppress `missed` rows
        # fleet-wide, and nothing else records who did it.
        log.warning(
            "cron: %s (%s) changed the control-plane timezone %s -> %s; re-phasing every cron anchor",
            actor.email, actor.id, zone_before, zone_after,
        )
        cron_reconcile.apply_timezone_change(session)
    # The same expression as the GET, not a literal `True`, so the two cannot drift if the gate widens.
    return _settings_out(session, actor.org_id, operator=is_operator(actor))


class UnroutedOrgOut(BaseModel):
    id: UUID
    name: str


class UnroutedNotificationsOut(BaseModel):
    """Task notifications that reached no one in the last `UNROUTED_WINDOW_DAYS` because nothing is set up to carry
    them (`task_notify.NO_ROUTE`): how many, the newest, and the orgs they were in (most first), so Settings can
    say it once and the operator knows whose setup it is."""
    count: int
    last_at: Optional[datetime] = None
    last_task_id: Optional[int] = None
    window_days: int
    orgs: list[UnroutedOrgOut] = []


UNROUTED_WINDOW_DAYS = 7


@router.get("/unrouted-notifications", response_model=UnroutedNotificationsOut)
@feature("system", "view")
def get_unrouted_notifications(
    user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
) -> UnroutedNotificationsOut:
    """The Settings notice for notifications nobody could receive (once here, not per task). A tenant
    reads its own org's; the operator every org's."""
    from app import task_notify

    since = datetime.now(timezone.utc) - timedelta(days=UNROUTED_WINDOW_DAYS)
    where = [ActivityEvent.kind == task_notify.UNROUTED_KIND, ActivityEvent.created_at >= since]
    if not is_operator(user):
        where.append(ActivityEvent.org_id == user.org_id)
    count = session.scalar(select(func.count()).select_from(ActivityEvent).where(*where)) or 0
    last = session.execute(
        select(ActivityEvent.created_at, ActivityEvent.data).where(*where)
        .order_by(ActivityEvent.created_at.desc()).limit(1)
    ).first()
    task_id = (last.data or {}).get("id") if last is not None else None
    per_org = func.count(ActivityEvent.id)
    orgs = session.execute(
        select(Org.id, Org.name).select_from(ActivityEvent).join(Org, Org.id == ActivityEvent.org_id).where(*where)
        .group_by(Org.id, Org.name).order_by(per_org.desc(), Org.name)
    ).all()
    return UnroutedNotificationsOut(
        count=count, last_at=last.created_at if last is not None else None,
        last_task_id=task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else None,
        window_days=UNROUTED_WINDOW_DAYS,
        orgs=[UnroutedOrgOut(id=org_id, name=name) for org_id, name in orgs],
    )


# ── The per-provider runtime switch (R14): ACP host or legacy path, per (provider, auth kind) ─────────
# Resolution and storage live in `app/acp_runtime.py`. Operator only, read and write.


class AcpRuntimeSwitch(BaseModel):
    """One (provider, auth kind) pair, or a switched launch lane (`acp_runtime.EXTRA_LANES`, its `auth_kind` the
    lane). `legacy_only`: no switch, always the terminal path, for `reason`. `acp_only`: no switch,
    always the ACP runtime, for `reason`."""

    provider: str
    label: str
    auth_kind: str
    #: `"<provider>:<auth_kind>"`, the key `PUT` takes.
    key: str
    on: bool
    legacy_only: bool
    acp_only: bool = False
    reason: str = ""


class AcpRuntimeOut(BaseModel):
    switches: list[AcpRuntimeSwitch]


class AcpRuntimeIn(StrictModel):
    #: `{"<provider>:<auth_kind>": on}`; keys left out keep their value.
    switches: dict[str, bool]


def _acp_runtime_out(values: dict[str, bool]) -> AcpRuntimeOut:
    from app import acp_runtime, agent_providers, provider_auth_policy

    rows = []
    for provider, kind in provider_auth_policy.supported_pairs():
        key = acp_runtime.pair_key(provider, kind)
        legacy_only = (provider, kind) in acp_runtime.LEGACY_ONLY
        acp_only = (provider, kind) in acp_runtime.ACP_ONLY
        reason = (acp_runtime.legacy_only_reason(provider) if legacy_only
                  else acp_runtime.acp_only_reason(provider) if acp_only else "")
        rows.append(AcpRuntimeSwitch(
            provider=provider, label=agent_providers.by_value(provider).label, auth_kind=kind, key=key,
            on=acp_only or (not legacy_only and values.get(key, True)), legacy_only=legacy_only, acp_only=acp_only,
            reason=reason))
    for provider, lane in acp_runtime.EXTRA_LANES:
        key = acp_runtime.pair_key(provider, lane)
        rows.append(AcpRuntimeSwitch(
            provider=provider, label=agent_providers.by_value(provider).label, auth_kind=lane, key=key,
            on=values.get(key, True), legacy_only=False))
    return AcpRuntimeOut(switches=rows)


@router.get("/acp-runtime", response_model=AcpRuntimeOut)
@feature("system", "view")
def get_acp_runtime(
    _actor: AdminUser = Depends(require_operator), session: Session = Depends(get_session)
) -> AcpRuntimeOut:
    """Every provider and auth type with its runtime: ON runs the ACP host, OFF the legacy path."""
    from app import acp_runtime

    return _acp_runtime_out(acp_runtime.switches(session))


@router.put("/acp-runtime", response_model=AcpRuntimeOut)
@feature("system", "edit")
def put_acp_runtime(
    body: AcpRuntimeIn,
    _actor: AdminUser = Depends(require_operator),
    session: Session = Depends(get_session),
) -> AcpRuntimeOut:
    """Set some switches. 422 on a pair with no switch; nothing is written then. A running attempt keeps the
    runtime it started on; the next dispatch reads the new value."""
    from app import acp_runtime

    try:
        values = acp_runtime.set_switches(session, body.switches)
    except settings_errors.OperatorSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    settings_cache.invalidate_cache()
    return _acp_runtime_out(values)


# ── The public legal pages, as an operator previews them before publishing (`app/legal_pages.py`) ───


def _telegram_refusal(exc: telegram_bot.TelegramError, sentence: str, *, key: str | None = None) -> HTTPException:
    """A Telegram refusal as a 422. One fixed in the bot's credential (Telegram rejected the token, or the
    credential holds none) is a `detail` object: `TELEGRAM_TOKEN_REFUSED` and the sentence naming that next step.
    `key` names the knob a settings save was refused on, so the console says it on that row: an object then too."""
    detail: dict[str, str] = {"message": sentence}
    if exc.token_refused:
        if not exc.no_token:  # The no-token sentence already sends the operator to Credentials.
            detail["message"] += " Check the bot token in its credential (Credentials)."
        detail["code"] = TELEGRAM_TOKEN_REFUSED
    if key is not None:
        detail["key"] = key
    return HTTPException(status_code=422, detail=detail if len(detail) > 1 else sentence)


@telegram_router.get("/telegram-registration")
@feature("system", "view")
def get_telegram_webhook(_actor: AdminUser = Depends(require_operator)) -> dict:
    """Settings > Integrations > Telegram's webhook state, live from Telegram, so an operator can tell whether
    Re-register is needed. `designated: false` with no bot chosen; a Telegram refusal is a 422 naming it."""
    try:
        info = telegram_bot.webhook_info()
    except telegram_bot.TelegramError as exc:
        if exc.no_token:
            raise _telegram_refusal(exc, str(exc)) from None
        # Status 0 is a network failure or a timeout; any HTTP status is Telegram answering with a refusal.
        if not exc.status:
            log.warning("telegram: getWebhookInfo failed (%s)", exc)
            raise HTTPException(status_code=422, detail="Telegram did not answer. Try again in a minute.") from None
        refused = "the bot token" if exc.token_refused else "the webhook read"
        raise _telegram_refusal(exc, f"Telegram refused {refused} ({exc}).") from None
    return {"designated": False} if info is None else {"designated": True, **info}


@telegram_router.post("/telegram-registration", status_code=204)
@feature("system", "edit")
def reregister_telegram_webhook(_actor: AdminUser = Depends(require_operator)) -> None:
    """Settings > Integrations > Telegram's Re-register: point the designated bot at this deployment's webhook
    again (after a token rotation, or a webhook another deployment took), without re-saving the setting."""
    designated = settings_reads.get_text(telegram_bot.SETTING_KEY)
    if not designated:
        raise HTTPException(status_code=422, detail="Pick a Telegram bot first.")
    try:
        telegram_bot.register_webhook(designated)
    except telegram_bot.TelegramError as exc:
        raise _telegram_refusal(exc, str(exc)) from None


@router.get("/legal-preview/{slug}")
@feature("system", "view")
def get_legal_preview(slug: str, _actor: AdminUser = Depends(require_operator)):
    """One legal page for an operator, published or not: `draft` says whether the public sees it yet and
    `missing` names the Settings > Legal values still unset. The public read is `GET /api/public/legal/{slug}`."""
    from app import legal_pages

    page = legal_pages.render(slug, preview=True)
    if page is None:
        raise HTTPException(status_code=404, detail="No such legal page.")
    return page


@router.get("/legal-status")
@feature("system", "view")
def get_legal_status(_actor: AdminUser = Depends(require_operator)):
    """Settings > Legal's status line: whether the pages are live, and every page whose text changed after the
    `legal_version` they show as Last updated (`legal_pages.changed_since`)."""
    from app import legal_pages

    last_updated = legal_pages.current_values()["last_updated"]
    return {
        "live": legal_pages.is_live(),
        "last_updated": last_updated,
        "changed_since": legal_pages.changed_since(last_updated),
    }
