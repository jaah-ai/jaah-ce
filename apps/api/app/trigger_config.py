"""`task_triggers.source_config` — one pydantic shape per source kind.

Every kind is savable from this child on, so the console can create any of them; a kind's ingest arrives with
its own child (GitHub, Slack, Gmail). `validate_source_config` is the one validator: the console API calls it
on create and on every edit, and its message is the readable 422.
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from fastapi import HTTPException

from app import access
from app.models.orgs import ACCOUNT_TYPES, SIGNUP_PROVIDERS
from app.production_qa.vocabulary import SEVERITIES
from app.triggers import (
    SOURCE_BITBUCKET,
    AUTH_API_KEY,
    SOURCE_AZURE_DEVOPS,
    SOURCE_CLOUDWATCH,
    SOURCE_DATADOG,
    SOURCE_DEPLOY_FAILED,
    SOURCE_GITHUB,
    SOURCE_GITLAB,
    SOURCE_GMAIL,
    SOURCE_HEALTH_BREACH,
    SOURCE_INCIDENT,
    SOURCE_PAGERDUTY,
    SOURCE_SENTRY,
    SOURCE_SLACK,
    SOURCE_USER_REGISTERED,
    SOURCE_WEBHOOK,
    TRACKER_SOURCE_KINDS,
    USER_MOMENT_APPLIED,
    USER_MOMENTS,
    USER_SCOPE_ORG,
    USER_SCOPE_PLATFORM,
    USER_SCOPES,
)

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# A GitLab project path: a namespace (subgroups included) and the project.
_GITLAB_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")
# The GitLab webhook events deploy ingestion reads (`app/production_qa/gitlab_deploys.py`).
GITLAB_DEPLOY_EVENTS = ("Push Hook", "Pipeline Hook", "Job Hook", "Deployment Hook")
# An Azure DevOps repository as `app/forges.py::RepoRef.path` names it: `organization/project/repo`.
_AZURE_SEGMENT = r"[^/\s<>\"\x00-\x1f][^/<>\"\x00-\x1f]{0,199}"
_AZURE_REPOSITORY = re.compile(f"{_AZURE_SEGMENT}/{_AZURE_SEGMENT}/{_AZURE_SEGMENT}")
_SLACK_CHANNEL = re.compile(r"[CGD][A-Z0-9]{2,30}")


def _path_validator(field: str, pattern: re.Pattern[str], message: str) -> Any:
    """A `field_validator` for a forge's repository path: stripped, and matching `pattern` or refused with `message`.
    One per forge spelling (`_REPOSITORY`, `_GITLAB_PATH`, `_AZURE_REPOSITORY`), assigned in the config's class body."""
    @field_validator(field)
    def validate(cls: Any, value: str) -> str:
        value = value.strip()
        if not pattern.fullmatch(value):
            raise ValueError(message)
        return value
    return validate


class Shape(BaseModel):
    """Every source config: unknown keys refused, frozen once read. The generic incident shape reuses it."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WebhookConfig(Shape):
    """`api_key`: `Authorization: Bearer jaah_…`, a Jaah API key of the trigger's org. Legacy, kept by a trigger saved
    with one (`routes/triggers.py::_keep_or_switch_auth`): `hmac`, the sender signs the raw body
    (`X-Jaah-Signature-256: sha256=<hex>`); `bearer`, `Authorization: Bearer <secret>`."""

    auth: Literal["api_key", "hmac", "bearer"] = AUTH_API_KEY
    # `incident`: the body is the generic monitor shape (`app/production_qa/adapters/generic.py`) and the trigger only
    # records it as an incident — it files no task (`app/production_qa/recorder.py::records_only`).
    shape: Literal["event", "incident"] = "event"


class GithubConfig(Shape):
    """`record_only`: the trigger records deploy facts for Production QA and never files a task, so it never adopts
    an automation (`app/production_qa/recorder.py::records_only`)."""

    events: list[str] = Field(min_length=1, max_length=50)
    actions: list[str] = Field(default_factory=list, max_length=50)
    repository: Optional[str] = None
    record_only: bool = False

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


class GitlabConfig(Shape):
    """A GitLab deploy webhook (`app/production_qa/gitlab_deploys.py`): it only records, so it never adopts an
    automation (`app/production_qa/recorder.py::records_only`). `repository` is the GitLab project path it listens
    to, required: a token that recorded any project's hooks would let one project's sender write another's deploys."""

    events: list[str] = Field(min_length=1, max_length=len(GITLAB_DEPLOY_EVENTS))
    repository: str
    record_only: Literal[True] = True

    @field_validator("events")
    @classmethod
    def _events(cls, values: list[str]) -> list[str]:
        unknown = [v for v in values if v not in GITLAB_DEPLOY_EVENTS]
        if unknown:
            raise ValueError(f"each event must be one of {', '.join(GITLAB_DEPLOY_EVENTS)}")
        return list(dict.fromkeys(values))

    _repository = _path_validator("repository", _GITLAB_PATH, "must look like group/project")


# What a Bitbucket deploy recorder listens to (`app/production_qa/bitbucket.py`): the push ledger and the build statuses.
BITBUCKET_DEPLOY_EVENTS = ("repo:push", "repo:commit_status_created", "repo:commit_status_updated")
_BRANCH = re.compile(r"[^\s~^:?*\[\\]{1,255}")


class BitbucketConfig(Shape):
    """A record-only Bitbucket Cloud repository webhook. `production_branch`: the branch whose build statuses are
    production deploys; `statuses`: the build-status keys or names that deploy there (empty: every status on it)."""

    events: list[Literal["repo:push", "repo:commit_status_created", "repo:commit_status_updated"]] = Field(
        default_factory=lambda: list(BITBUCKET_DEPLOY_EVENTS), min_length=1, max_length=3)
    repository: str
    production_branch: str
    statuses: list[str] = Field(default_factory=list, max_length=50)

    _repository = _path_validator("repository", _REPOSITORY, "must be the workspace and repository slugs joined by a slash")

    @field_validator("production_branch")
    @classmethod
    def _branch(cls, value: str) -> str:
        value = value.strip()
        if not _BRANCH.fullmatch(value) or value.startswith(("/", "-")) or value.endswith(("/", ".")) or ".." in value:
            raise ValueError("must be a branch name such as main")
        return value

    @field_validator("statuses")
    @classmethod
    def _statuses(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values if v.strip()]
        if any(len(v) > 255 for v in cleaned):
            raise ValueError("each status must be at most 255 characters")
        return list(dict.fromkeys(cleaned))


class GmailConfig(Shape):
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


class SlackConfig(Shape):
    channel_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("channel_ids")
    @classmethod
    def _channels(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        bad = [v for v in cleaned if not _SLACK_CHANNEL.fullmatch(v)]
        if bad:
            raise ValueError(f"{bad[0]!r} is not a Slack channel id (like C0123ABCD)")
        return cleaned


class UserRegisteredConfig(Shape):
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


class SentryMetricsTarget(Shape):
    project_slug: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=100)


class SentryMetrics(Shape):
    """Where the post-deploy regression check (C7) reads this monitor's numbers: validated here (shape only — the
    reader scopes `credential_id` to the trigger's org), read there."""

    credential_id: UUID
    sentry: SentryMetricsTarget


class SentryConfig(Shape):
    """`environments`: record only events from these Sentry environments (empty: every one; an `issue` webhook names
    none, so it is always recorded). `metrics`: the regression check's source (`SentryMetrics`)."""

    environments: list[str] = Field(default_factory=list, max_length=50)
    metrics: Optional[SentryMetrics] = None

    @field_validator("environments")
    @classmethod
    def _environments(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v or len(v) > 100 for v in cleaned):
            raise ValueError("each environment must be 1 to 100 characters")
        return list(dict.fromkeys(cleaned))


class DatadogMetricsTarget(Shape):
    errors_query: str = Field(min_length=1, max_length=2000)
    hits_query: str = Field(min_length=1, max_length=2000)
    p95_query: str = Field(min_length=1, max_length=2000)


class DatadogMetrics(Shape):
    """The regression check's Datadog source (`SentryMetrics`' twin): validated here, read there."""

    credential_id: UUID
    datadog: DatadogMetricsTarget


class DatadogConfig(Shape):
    """`metrics`: the regression check's source (`DatadogMetrics`). Nothing else: what Datadog sends is the owner's
    payload template (`app/production_qa/adapters/datadog.py`)."""

    metrics: Optional[DatadogMetrics] = None


class AzureDevopsConfig(Shape):
    """`repository`: the `organization/project/repo` whose deploys it records (`app/production_qa/azure_devops.py`).
    Always record-only: an Azure DevOps trigger never files a task."""

    repository: str
    record_only: Literal[True] = True

    _repository = _path_validator("repository", _AZURE_REPOSITORY, "must look like organization/project/repo")


class TrackerConfig(Shape):
    """A tracker connection's own trigger (`app/trackers/`): the connection it carries events for. Written by the
    connection alone (`trackers.connections.connect`); its mappings and start label live on the connection."""

    connection_id: UUID


class PagerdutyConfig(Shape):
    """No settings: the subscription's secret is the trigger's own, and every incident it sends is recorded."""


_AWS_REGION = r"[a-z]{2}(?:-[a-z]+)+-\d"
# An SNS topic's ARN: partition, region, 12-digit account, topic name (`adapters/cloudwatch.py` pins the sender to it).
_TOPIC_ARN = re.compile(rf"arn:aws(?:-cn|-us-gov)?:sns:{_AWS_REGION}:\d{{12}}:[A-Za-z0-9_-]{{1,256}}(?:\.fifo)?")


class MetricDimension(Shape):
    name: str = Field(min_length=1, max_length=255)
    value: str = Field(min_length=1, max_length=1024)


class MetricRef(Shape):
    """One CloudWatch metric: what `GetMetricData` names."""

    namespace: str = Field(min_length=1, max_length=255)
    metric_name: str = Field(min_length=1, max_length=255)
    dimensions: list[MetricDimension] = Field(default_factory=list, max_length=30)
    stat: str = Field(min_length=1, max_length=50)


class CloudwatchMetricsTarget(Shape):
    region: str = Field(pattern=rf"^{_AWS_REGION}$")
    errors: MetricRef
    requests: MetricRef
    latency: Optional[MetricRef] = None


class CloudwatchMetrics(Shape):
    """The regression check's CloudWatch source (`SentryMetrics`' twin): validated here, read there."""

    credential_id: UUID
    cloudwatch: CloudwatchMetricsTarget


class CloudwatchConfig(Shape):
    """`topic_arns`: the SNS topics whose messages are accepted (required — a validly signed message from any other
    topic, even another AWS account's, is refused). `severity`: what every alarm of these topics records as (CloudWatch
    sends none). `metrics`: the regression check's source (`CloudwatchMetrics`)."""

    topic_arns: list[str] = Field(min_length=1, max_length=20)
    severity: str = "error"
    metrics: Optional[CloudwatchMetrics] = None

    @field_validator("topic_arns")
    @classmethod
    def _topic_arns(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not _TOPIC_ARN.fullmatch(v) for v in cleaned):
            raise ValueError("each must be an SNS topic ARN (arn:aws:sns:<region>:<account>:<topic>)")
        return list(dict.fromkeys(cleaned))

    @field_validator("severity")
    @classmethod
    def _severity(cls, value: str) -> str:
        from app.production_qa.vocabulary import SEVERITIES

        if value not in SEVERITIES:
            raise ValueError(f"one of {', '.join(SEVERITIES)}")
        return value

_CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


class QuietHours(Shape):
    """A daily window, local to `tz`, in which an incident's task waits (`held`) instead of filing; `start` after
    `end` spans midnight. Equal ends are no window at all and are refused."""

    start: str
    end: str
    tz: str = Field(min_length=1, max_length=64)

    @field_validator("start", "end")
    @classmethod
    def _clock(cls, value: str) -> str:
        if not _CLOCK.fullmatch(value):
            raise ValueError("must be HH:MM on a 24-hour clock")
        return value

    @field_validator("tz")
    @classmethod
    def _zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{value!r} is not a time zone (like Europe/London)") from None
        return value

    @model_validator(mode="after")
    def _a_window(self) -> "QuietHours":
        if self.start == self.end:
            raise ValueError("start and end are the same time — leave quiet hours off instead")
        return self


class IncidentWhenConfig(Shape):
    """The When of an automation on a CP-raised incident kind (`app/production_qa/events.py`, architecture §6.2).
    `min_severity`: the lowest severity that files (`vocabulary.SEVERITIES`). `types`: the incident types this kind
    files for (empty: every type the kind carries, `TYPES`). `environments`: null is production only (the project's
    production environment) for a type that carries one, every environment for a failed deploy (D5); a list names
    them. `quiet_hours`: null files at once."""

    # The incident types an automation of this kind may select; the first kind's are routed to it (§6.1).
    TYPES: ClassVar[tuple[str, ...]] = ("monitor", "regression", "lane_slowdown")

    min_severity: Literal["debug", "info", "warning", "error", "critical"] = "error"
    types: list[str] = Field(default_factory=list, max_length=5)
    environments: Optional[list[str]] = Field(default=None, max_length=50)
    quiet_hours: Optional[QuietHours] = None

    @field_validator("types")
    @classmethod
    def _types(cls, values: list[str]) -> list[str]:
        unknown = [v for v in values if v not in cls.TYPES]
        if unknown:
            raise ValueError(f"{', '.join(unknown)}: not a type this kind carries — one of {', '.join(cls.TYPES)}")
        return list(dict.fromkeys(values))

    @field_validator("environments")
    @classmethod
    def _environments(cls, values: Optional[list[str]]) -> Optional[list[str]]:
        if values is None:
            return None
        cleaned = [v.strip() for v in values]
        if any(not v or len(v) > 100 for v in cleaned):
            raise ValueError("each environment must be 1 to 100 characters")
        return list(dict.fromkeys(cleaned)) or None


class DeployFailedWhenConfig(IncidentWhenConfig):
    TYPES: ClassVar[tuple[str, ...]] = ("deploy_failed",)


class HealthBreachWhenConfig(IncidentWhenConfig):
    TYPES: ClassVar[tuple[str, ...]] = ("health_breach",)


assert IncidentWhenConfig.model_fields["min_severity"].default in SEVERITIES

SHAPES: dict[str, type[Shape]] = {
    SOURCE_WEBHOOK: WebhookConfig,
    SOURCE_GITHUB: GithubConfig,
    SOURCE_GITLAB: GitlabConfig,
    SOURCE_BITBUCKET: BitbucketConfig,
    SOURCE_GMAIL: GmailConfig,
    SOURCE_SLACK: SlackConfig,
    SOURCE_USER_REGISTERED: UserRegisteredConfig,
    SOURCE_SENTRY: SentryConfig,
    SOURCE_DATADOG: DatadogConfig,
    SOURCE_PAGERDUTY: PagerdutyConfig,
    SOURCE_CLOUDWATCH: CloudwatchConfig,
    SOURCE_INCIDENT: IncidentWhenConfig,
    SOURCE_DEPLOY_FAILED: DeployFailedWhenConfig,
    SOURCE_HEALTH_BREACH: HealthBreachWhenConfig,
    SOURCE_AZURE_DEVOPS: AzureDevopsConfig,
    **{kind: TrackerConfig for kind in TRACKER_SOURCE_KINDS},
}


def user_scope_of(raw: Any) -> str:
    """The `scope` a raw `user_registered` config names, before validation: the placement rule reads it
    (`routes/triggers.py::_source_allowed_or_refuse`) and an unset one is the default, `org`."""
    if isinstance(raw, dict) and raw.get("scope") == USER_SCOPE_PLATFORM:
        return USER_SCOPE_PLATFORM
    return USER_SCOPE_ORG


class SourceConfigError(ValueError):
    """The readable reason a source config cannot be saved."""


def first_error(exc: ValidationError) -> str:
    """The first validation error as `field: message` — the one readable form a refused shape is shown with."""
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
        raise SourceConfigError(first_error(exc)) from None
    return parsed.model_dump(mode="json", by_alias=True)
