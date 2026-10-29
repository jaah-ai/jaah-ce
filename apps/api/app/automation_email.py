"""The automation action "Send email" (`email.send`): when its When fires, one message goes out from
the org's connected Gmail through the tenant send seam (`app/tenant_gmail/` `send`, S7), recorded against the run.

Params (`EMAIL_PARAM_DEFAULTS`): `connection_id` (the Gmail connection to send from; None = the org's only one), and
the templates `to`, `cc` (addresses separated by commas, semicolons or spaces), `subject` and `body` (plain text).

Templating is plain substitution, never evaluation. A placeholder is `{{name}}`, the Task action's syntax:
  * on every run, `OWN_FIELDS`: `{{automation.name}}`, `{{fired_at}}` (UTC) and `{{org.name}}`;
  * on an event run, also the trigger's own fields (`{{body.x}}`, `{{email.subject}}`, …), rendered by
    `app/trigger_template.py` exactly as a Task action's body is.
A placeholder with no value renders empty; a credential header (`trigger_template.FORBIDDEN_HEADERS`) is refused at save.
The rendered Subject is flattened onto one line (`mail_message.header_text`). Recipients are split from the TEMPLATE,
before rendering, and each token must render to exactly one address (`mail_message.single_address`): an event's data
can neither inject a header nor turn one placeholder into a recipient list.

The engine runs it with no Target (`automation_fire._fire_untargeted`): one message per firing.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NoReturn, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features, mail_message, plan_terms, trigger_template
from app.automations import OUTCOME_FILED, WHEN_EVENT
from app.entity_shape import optional_uuid

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from app.automation_actions import AutomationDraft
    from app.automation_fire import FireContext, TargetOutcome

#: Exactly the keys a Send email action's params carry, with the value an absent key reads as.
EMAIL_PARAM_DEFAULTS: dict[str, Any] = {"connection_id": None, "to": "", "cc": "", "subject": "", "body": ""}
#: The placeholders every run fills, whatever its When.
OWN_FIELDS: tuple[str, ...] = ("automation.name", "fired_at", "org.name")

MAX_RECIPIENTS_CHARS = 2_000
MAX_SUBJECT_CHARS = 250
MAX_BODY_CHARS = 20_000

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
#: One recipient token of a To/Cc template: a run of non-separators, a placeholder (spaces and all) kept whole.
_TOKEN = re.compile(r"(?:\{\{.*?\}\}|[^,;\s])+", re.DOTALL)


def _refuse(field_name: str, message: str) -> NoReturn:
    raise HTTPException(status_code=422, detail=f"action.params.{field_name}: {message}")


# ── Save ─────────────────────────────────────────────────────────────────────────────────────────────


def validate(session: Session, draft: "AutomationDraft", params: dict[str, Any]) -> dict[str, Any]:
    """Normalise and check a Send email action's params. No connection yet is allowed: a firing records it."""
    if not isinstance(params, dict):
        _refuse("", "must be an object")
    unknown = sorted(set(params) - set(EMAIL_PARAM_DEFAULTS))
    if unknown:
        _refuse(unknown[0], f"is not a Send email field; expected one of {', '.join(EMAIL_PARAM_DEFAULTS)}")
    if not plan_terms.switch_on(session, draft.org_id, plan_terms.TENANT_EMAIL):
        raise HTTPException(status_code=422, detail=f"action.type: {plan_terms.not_on_plan(plan_terms.TENANT_EMAIL)}")
    out = {**EMAIL_PARAM_DEFAULTS, **params}
    for name, limit in (("to", MAX_RECIPIENTS_CHARS), ("cc", MAX_RECIPIENTS_CHARS), ("subject", MAX_SUBJECT_CHARS),
                        ("body", MAX_BODY_CHARS)):
        value = out[name] if out[name] is not None else ""
        if not isinstance(value, str):
            _refuse(name, "must be text")
        if len(value) > limit:
            _refuse(name, f"is {len(value)} characters; the limit is {limit}")
        _check_placeholders(name, value, draft.when_kind)
        out[name] = value.strip() if name != "body" else value
    for name in ("to", "subject", "body"):
        if not out[name].strip():
            _refuse(name, "is required")
    literal = 0
    for name in ("to", "cc"):
        for token in _tokens(out[name]):
            if _PLACEHOLDER.search(token):
                continue
            if mail_message.single_address(token) is None:
                _refuse(name, f"{token[:80]!r} is not an email address")
            literal += 1
    if literal > _max_recipients():
        _refuse("to", f"a message goes to at most {_max_recipients()} recipients")
    out["connection_id"] = _connection_or_refuse(session, draft, out["connection_id"])
    return out


def _max_recipients() -> int:
    if not edition_features.enabled("tenant_mail"):
        raise _not_in_edition()
    from app.tenant_gmail import MAX_RECIPIENTS

    return MAX_RECIPIENTS


def _not_in_edition() -> HTTPException:
    """Send email's 404 in an edition without the org's connected Gmail (`ce_caps.NOT_IN_CE`)."""
    from app.ce_caps import NOT_IN_CE

    return HTTPException(status_code=404, detail=NOT_IN_CE)


def _check_placeholders(field_name: str, template: str, when_kind: str) -> None:
    for match in _PLACEHOLDER.finditer(template):
        inner = match.group(1).strip()
        if inner in OWN_FIELDS:
            continue
        if when_kind != WHEN_EVENT:
            _refuse(field_name, f"{{{{{inner}}}}} is not a field a schedule fills; use "
                                + ", ".join(f"{{{{{name}}}}}" for name in OWN_FIELDS))
        try:
            (placeholder,) = trigger_template.placeholders(match.group(0))
        except trigger_template.TemplateError as exc:
            _refuse(field_name, f"{exc}; or one of " + ", ".join(f"{{{{{name}}}}}" for name in OWN_FIELDS))
        if placeholder.namespace == "headers" and placeholder.steps:
            header = str(placeholder.steps[0]).lower()
            if header in trigger_template.FORBIDDEN_HEADERS or header.startswith(trigger_template.FORBIDDEN_HEADER_PREFIXES):
                _refuse(field_name, f"{{{{{inner}}}}} is a sender's credential; it can never be emailed")


def _connection_or_refuse(session: Session, draft: "AutomationDraft", value: Any) -> Optional[str]:
    """The connection to send from: one of the org's, or None for its only one (refused while it has several). An id
    already stored on this automation survives a disconnect, so the automation stays editable; its firing records
    the refusal."""
    from app.models.automations import Automation

    if not edition_features.enabled("tenant_mail"):
        raise _not_in_edition()
    from app.tenant_gmail import connection, connections

    if value in (None, ""):
        if len(connections(session, draft.org_id)) > 1:
            _refuse("connection_id", "this organization has several Gmail connections; pick the one to send from")
        return None
    connection_id = optional_uuid(value)
    if connection_id is None:
        _refuse("connection_id", "must be a Gmail connection id")
    if connection(session, draft.org_id, connection_id) is None:
        stored = None
        if draft.automation_id is not None:
            stored = (session.scalar(select(Automation.action_params).where(Automation.id == draft.automation_id))
                      or {}).get("connection_id")
        if stored != str(connection_id):
            _refuse("connection_id", "is not a Gmail connection of this organization")
    return str(connection_id)


def _tokens(recipients: str) -> list[str]:
    return _TOKEN.findall(recipients or "")


# ── Fire ─────────────────────────────────────────────────────────────────────────────────────────────


def render(template: str, fields: dict[str, str], event: Optional[dict[str, Any]]) -> tuple[str, list[str]]:
    """`template` with every placeholder substituted: an own field from `fields`, any other from `event` (an event run;
    None on a schedule, where only own fields were allowed at save). Returns (text, missing placeholders)."""
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if inner in fields:
            return fields[inner]
        if event is None:
            missing.append(inner)
            return ""
        rendered = trigger_template.render(match.group(0), event)
        missing.extend(path for path in rendered.missing_paths if path not in missing)
        return rendered.text

    return _PLACEHOLDER.sub(substitute, template or ""), missing


def own_fields(session: Session, ctx: "FireContext") -> dict[str, str]:
    from app.models.orgs import Org

    org_name = session.scalar(select(Org.name).where(Org.id == ctx.automation.org_id)) or ""
    fired_at = (ctx.fired_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {"automation.name": ctx.automation.name or "", "fired_at": fired_at.strftime("%Y-%m-%d %H:%M UTC"),
            "org.name": org_name}


def _recipients(template: str, field_name: str, fields: dict[str, str], event: Optional[dict[str, Any]],
                missing: list[str]) -> list[str]:
    """The addresses `template` names: each token of the template rendered on its own, and refused unless it is
    exactly one address — so a value carrying separators is one bad address, never several. A token rendering empty
    names nobody."""
    from app.automation_fire import REASON_SEND_FAILED, TargetFailed

    out: list[str] = []
    for token in _tokens(template):
        text, gaps = render(token, fields, event)
        missing += [gap for gap in gaps if gap not in missing]
        if not text.strip():
            continue
        address = mail_message.single_address(text)
        if address is None:
            raise TargetFailed(REASON_SEND_FAILED.format(
                error=f"{field_name} rendered {mail_message.header_text(text, limit=80)!r}, not one email address"))
        if address.lower() not in {a.lower() for a in out}:
            out.append(address)
    return out


def fire(ctx: "FireContext", _target: Any) -> "TargetOutcome":
    """Render the message and send it as the automation's owner; raises `TargetFailed` for anything not sent."""
    from app.automation_fire import REASON_SEND_FAILED, TargetFailed, TargetOutcome

    if not edition_features.enabled("tenant_mail"):
        raise TargetFailed(REASON_SEND_FAILED.format(error=_not_in_edition().detail))
    from app import tenant_gmail

    session, params = ctx.session, ctx.params
    fields = own_fields(session, ctx)
    event = (ctx.event or {}) if ctx.renders else None
    missing: list[str] = []
    rendered: dict[str, str] = {}
    for name in ("subject", "body"):
        rendered[name], gaps = render(params.get(name) or "", fields, event)
        missing += [gap for gap in gaps if gap not in missing]
    to = _recipients(params.get("to") or "", "To", fields, event, missing)
    cc = _recipients(params.get("cc") or "", "Cc", fields, event, missing)
    if not to:
        raise TargetFailed(REASON_SEND_FAILED.format(error="To rendered no recipient"))
    subject = mail_message.header_text(rendered["subject"])
    if not subject:
        raise TargetFailed(REASON_SEND_FAILED.format(error="the subject rendered empty"))
    message = tenant_gmail.TenantOutboundEmail(to=to, cc=[a for a in cc if a.lower() not in {t.lower() for t in to}],
                                               subject=subject, text=rendered["body"])
    connection_id = UUID(params["connection_id"]) if params.get("connection_id") else None
    try:
        result = tenant_gmail.send(
            session, org_id=ctx.automation.org_id, connection_id=connection_id, message=message,
            actor=tenant_gmail.Actor.person(ctx.owner),
            # A schedule's run is a `cron_runs` uuid; an event's is its delivery's integer id.
            automation_run_id=ctx.run_id if ctx.run_column == "cron_run_id" else None,
            trigger_delivery_id=ctx.run_id if ctx.run_column == "delivery_id" else None,
        )
    except tenant_gmail.TenantEmailError as exc:
        raise TargetFailed(REASON_SEND_FAILED.format(error=str(exc))) from exc
    count = len(message.to) + len(message.cc)
    return TargetOutcome(OUTCOME_FILED, f"Sent email from {result.from_address} to {count} recipient(s): {subject}",
                         missing_paths=tuple(missing))
