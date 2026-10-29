"""`task_triggers.source_config` — one pydantic shape per source kind.

Every kind is savable from this child on, so the console can create any of them; a kind's ingest arrives with
its own child (GitHub, Slack, Gmail). `validate_source_config` is the one validator: the console API calls it
on create and on every edit, and its message is the readable 422.
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from fastapi import HTTPException

from app import access
from app.models.orgs import ACCOUNT_TYPES, SIGNUP_PROVIDERS
from app.triggers import (
    AUTH_API_KEY,
    SOURCE_GITHUB,
    SOURCE_GMAIL,
    SOURCE_SLACK,
    SOURCE_USER_REGISTERED,
    SOURCE_WEBHOOK,
    USER_MOMENT_APPLIED,
    USER_MOMENTS,
    USER_SCOPE_ORG,
    USER_SCOPE_PLATFORM,
    USER_SCOPES,
)

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SLACK_CHANNEL = re.compile(r"[CGD][A-Z0-9]{2,30}")


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WebhookConfig(_Shape):
    """`api_key`: `Authorization: Bearer jaah_…`, a Jaah API key of the trigger's org. Legacy, kept by a trigger saved
    with one (`routes/triggers.py::_keep_or_switch_auth`): `hmac`, the sender signs the raw body
    (`X-Jaah-Signature-256: sha256=<hex>`); `bearer`, `Authorization: Bearer <secret>`."""

    auth: Literal["api_key", "hmac", "bearer"] = AUTH_API_KEY


class GithubConfig(_Shape):
    events: list[str] = Field(min_length=1, max_length=50)
    actions: list[str] = Field(default_factory=list, max_length=50)
    repository: Optional[str] = None

    @field_validator("events", "actions")
    @classmethod
    def _names(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v or len(v) > 64 for v in cleaned):
            raise ValueError("each name must be 1 to 64 characters")
        return cleaned

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _REPOSITORY.fullmatch(value):
            raise ValueError("must look like owner/repo")
        return value


class GmailConfig(_Shape):
    """Case-insensitive substrings of the message's To, From and Subject; every one that is set must match (none set:
    every message at a hosted address or a connection matches).

    `inbox` is where it listens (seam S8): `system`, the deployment's own mail domain (operator org only,
    `routes/triggers.py`), or a tenant Gmail connection's id; `address_id` narrows it to one hosted address."""

    to: Optional[str] = Field(default=None, max_length=320)
    from_: Optional[str] = Field(default=None, max_length=320, alias="from")
    subject: Optional[str] = Field(default=None, max_length=500)
    inbox: Union[Literal["system"], UUID] = "system"
    address_id: Optional[UUID] = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _at_least_one(self) -> "GmailConfig":
        # A hosted address or a Gmail connection is the trigger's own source, so every message there may file one;
        # the deployment's shared domain needs a rule.
        if self.address_id is None and self.inbox == "system" and not any(
            (v or "").strip() for v in (self.to, self.from_, self.subject)
        ):
            raise ValueError("set at least one of to, from or subject — otherwise every email would match")
        return self


class SlackConfig(_Shape):
    channel_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("channel_ids")
    @classmethod
    def _channels(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        bad = [v for v in cleaned if not _SLACK_CHANNEL.fullmatch(v)]
        if bad:
            raise ValueError(f"{bad[0]!r} is not a Slack channel id (like C0123ABCD)")
        return cleaned


class UserRegisteredConfig(_Shape):
    """Whose registration fires it (`scope`), when a platform sign-up counts (`moment`), and the optional
    filters — each one that is set must match: the sign-in provider, the application's account type (platform
    sign-ups only) and the email's domain. Empty lists match everyone."""

    # The literals are the one home's tuples (`models.SIGNUP_PROVIDERS`, `models.ACCOUNT_TYPES`, `triggers.USER_*`).
    scope: Literal[USER_SCOPES] = USER_SCOPE_ORG
    moment: Literal[USER_MOMENTS] = USER_MOMENT_APPLIED
    providers: list[Literal[SIGNUP_PROVIDERS]] = Field(default_factory=list)
    account_types: list[Literal[ACCOUNT_TYPES]] = Field(default_factory=list)
    email_domains: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("email_domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        """Each as `access.email_domain` will answer it at match time (`normalized_domain`, the one domain
        rule), so a domain this accepts is one a registration can carry."""
        cleaned: list[str] = []
        for raw in values:
            try:
                cleaned.append(access.normalized_domain(raw.strip().lstrip("@")))
            except HTTPException:
                raise ValueError(f"{raw!r} is not a domain (like example.com)") from None
        return list(dict.fromkeys(cleaned))

    @field_validator("providers", "account_types")
    @classmethod
    def _distinct(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def _platform_only_fields(self) -> "UserRegisteredConfig":
        if self.scope == USER_SCOPE_ORG and self.moment != USER_MOMENT_APPLIED:
            raise ValueError("moment: only a platform sign-up has an approval — an org member counts when they join")
        if self.scope == USER_SCOPE_ORG and self.account_types:
            raise ValueError("account_types: only a platform sign-up has an account type")
        return self


SHAPES: dict[str, type[_Shape]] = {
    SOURCE_WEBHOOK: WebhookConfig,
    SOURCE_GITHUB: GithubConfig,
    SOURCE_GMAIL: GmailConfig,
    SOURCE_SLACK: SlackConfig,
    SOURCE_USER_REGISTERED: UserRegisteredConfig,
}


def user_scope_of(raw: Any) -> str:
    """The `scope` a raw `user_registered` config names, before validation: the placement rule reads it
    (`routes/triggers.py::_source_allowed_or_refuse`) and an unset one is the default, `org`."""
    if isinstance(raw, dict) and raw.get("scope") == USER_SCOPE_PLATFORM:
        return USER_SCOPE_PLATFORM
    return USER_SCOPE_ORG


class SourceConfigError(ValueError):
    """The readable reason a source config cannot be saved."""


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(p) for p in error.get("loc", ()) if p != "__root__")
    message = str(error.get("msg", "invalid")).removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


def validate_source_config(kind: str, raw: Any) -> dict[str, Any]:
    """The normalized config for `kind`, or `SourceConfigError` naming the field."""
    shape = SHAPES.get(kind)
    if shape is None:
        raise SourceConfigError(f"unknown source kind {kind!r}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SourceConfigError("must be a JSON object")
    try:
        parsed = shape.model_validate(raw)
    except ValidationError as exc:
        raise SourceConfigError(_first_error(exc)) from None
    return parsed.model_dump(mode="json", by_alias=True)
