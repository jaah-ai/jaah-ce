"""What a gate and a deploy target may hold (architecture §3.1, §3.2), the save's refusals, and the one-line summary
every surface shows (AC1.8: the UI never builds one).

A gate's `config` is per `kind`, validated by a pydantic discriminated union; the node driver reads the same JSON.
A refusal is a 422 `{code, message, stage_index, gate_index}` (`refuse`), worded for the person editing.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from app.forges import FORGE_LABELS
from app.models.production_qa import SEVERITIES
from app.models.releases import (
    GATE_KINDS,
    GATE_RETRIES_MAX,
    GATE_TIMEOUT_MAX,
    GATE_TIMEOUT_MIN,
    TARGET_KINDS,
)

#: Most gates one list holds: a bound on one save's write, not a product limit anyone meets.
GATES_MAX = 20
NAME_MAX = 80
RUN_MAX = 4000

HTTPS_MESSAGE = "Use an https:// address"
#: D8's exact words (AC7.3).
ASK_ON_AUTO_MESSAGE = "An automatic step can't ask for a ticket. Choose a fixed ticket or query."

#: What each forge calls its CI, for the summary line.
CI_LABELS: Mapping[str, str] = {
    "github": "GitHub Actions", "gitlab": "GitLab CI", "bitbucket": "Bitbucket Pipelines",
    "azure_devops": "Azure Pipelines",
}
#: A ticket gate reads its system with a login it names (the control plane holds no project-wide one).
TICKET_LOGIN_MESSAGE = "Pick the login it reads tickets with."
TICKET_LABELS: Mapping[str, str] = {"jira": "Jira", "servicenow": "ServiceNow"}


class Refused(Exception):
    """A config the save refuses: `code` and the sentence a person reads."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def refuse(code: str, message: str, stage_index: Optional[int] = None,
           gate_index: Optional[int] = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if stage_index is not None:
        detail["stage_index"] = stage_index
    if gate_index is not None:
        detail["gate_index"] = gate_index
    return HTTPException(status_code=422, detail=detail)


def _https(value: str) -> str:
    text = value.strip()
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.netloc:
        raise Refused("url_https", HTTPS_MESSAGE)
    return text


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandConfig(_Config):
    kind: Literal["command"] = "command"
    run: str = Field(min_length=1, max_length=RUN_MAX)

    @field_validator("run")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a command to run")
        return value.strip()


class CiConfig(_Config):
    kind: Literal["ci"] = "ci"
    mode: Literal["start", "wait"] = "wait"
    workflow: Optional[str] = Field(default=None, max_length=500)
    all_checks: bool = False

    @model_validator(mode="after")
    def _shape(self) -> "CiConfig":
        self.workflow = (self.workflow or "").strip() or None
        if self.all_checks and (self.mode != "wait" or self.workflow is not None):
            raise ValueError("all checks only waits, and names no workflow")
        if not self.all_checks and self.workflow is None:
            raise ValueError("a workflow to start or wait for")
        return self


class WebhookConfig(_Config):
    kind: Literal["webhook"] = "webhook"
    url: str = Field(min_length=1, max_length=2000)


class ProbeConfig(_Config):
    kind: Literal["probe"] = "probe"
    path: str = Field(default="/", max_length=500)
    expect_min: int = Field(default=200, ge=100, le=599)
    expect_max: int = Field(default=399, ge=100, le=599)
    contains: Optional[str] = Field(default=None, max_length=500)
    tries: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def _shape(self) -> "ProbeConfig":
        self.path = self.path.strip() or "/"
        if not self.path.startswith("/"):
            raise ValueError("a path starting with /")
        if self.expect_min > self.expect_max:
            raise ValueError("the lowest status must not be above the highest")
        self.contains = (self.contains or "") or None
        return self


class AlertsConfig(_Config):
    kind: Literal["alerts"] = "alerts"
    min_severity: Optional[Literal[SEVERITIES]] = None  # type: ignore[valid-type]
    environment: Optional[str] = Field(default=None, max_length=200)


class TicketState(_Config):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class TicketConfig(_Config):
    kind: Literal["ticket"] = "ticket"
    system: Literal["jira", "servicenow"]
    source: Literal["fixed", "ask"]
    query: Optional[str] = Field(default=None, max_length=2000)
    states: list[TicketState] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _shape(self) -> "TicketConfig":
        self.query = (self.query or "").strip() or None
        if self.source == "fixed" and self.query is None:
            raise ValueError("a ticket or query to check")
        return self


GateConfig = Annotated[
    Union[CommandConfig, CiConfig, WebhookConfig, ProbeConfig, AlertsConfig, TicketConfig],
    Field(discriminator="kind"),
]
_GATE_CONFIG: TypeAdapter[Any] = TypeAdapter(GateConfig)


class GateIn(BaseModel):
    """One gate as a save sends it (`Gate` minus `summary` and `warning`); `id` None is a new one. Loose on purpose:
    `parse_gate` judges it and answers the save's own 422."""

    id: Optional[str] = None
    name: str = ""
    kind: str
    enabled: bool = True
    required: bool = True
    timeout_s: int = 600
    retries: int = 0
    config: dict[str, Any] = Field(default_factory=dict)
    credential: Optional[str] = None


class TargetIn(BaseModel):
    kind: str = "none"
    config: Optional[dict[str, Any]] = None
    credential: Optional[str] = None


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    message = str(error.get("msg") or "invalid")
    return message.removeprefix("Value error, ")


def _credential(value: Optional[str]) -> Optional[str]:
    """The path's shape only; whether the saving person may use it is the save's check
    (`app/releases/pipeline.py::refuse_unusable_credentials`)."""
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > 300 or not text.startswith(("org/", "projects/", "me/")):
        raise Refused("credential_invalid", "Pick a saved credential.")
    return text


def parse_gate(gate: GateIn, *, auto_step: bool) -> dict[str, Any]:
    """A sent gate as stored columns, or `Refused`."""
    name = gate.name.strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise Refused("gate_name", f"Name each check (1 to {NAME_MAX} characters).")
    if gate.kind not in GATE_KINDS:
        raise Refused("gate_kind", "Unknown kind of check.")
    if not GATE_TIMEOUT_MIN <= gate.timeout_s <= GATE_TIMEOUT_MAX:
        raise Refused("timeout_range",
                      f"Time limit must be {GATE_TIMEOUT_MIN // 60} to {GATE_TIMEOUT_MAX // 60} minutes.")
    if not 0 <= gate.retries <= GATE_RETRIES_MAX:
        raise Refused("retries_range", f"Retries must be 0 to {GATE_RETRIES_MAX}.")
    raw = {**gate.config, "kind": gate.kind}
    if gate.kind == "webhook" and isinstance(raw.get("url"), str):
        raw["url"] = _https(raw["url"])
    try:
        config = _GATE_CONFIG.validate_python(raw)
    except ValidationError as exc:
        raise Refused("gate_config", f"{name}: {_first_error(exc)}.") from None
    credential = _credential(gate.credential)
    if isinstance(config, WebhookConfig) and credential is None:
        raise Refused("credential_required", "A webhook needs its signing secret.")
    if isinstance(config, TicketConfig) and credential is None:
        raise Refused("credential_required", TICKET_LOGIN_MESSAGE)
    if isinstance(config, TicketConfig) and config.source == "ask" and auto_step:
        raise Refused("ticket_ask_auto", ASK_ON_AUTO_MESSAGE)
    stored = config.model_dump(mode="json", exclude={"kind"})
    return {"name": name, "kind": gate.kind, "enabled": gate.enabled, "required": gate.required,
            "timeout_s": gate.timeout_s, "retries": gate.retries, "config": stored, "credential": credential}


def parse_target(target: TargetIn) -> dict[str, Any]:
    """A sent deploy target as the stage's columns (`target_kind`, `target_config`, `target_credential`), or
    `Refused`. `forge_env` keeps using the stage's `deploy_environment`, never a copy."""
    if target.kind not in TARGET_KINDS:
        raise Refused("target_kind", "Unknown deploy target.")
    raw = dict(target.config or {})
    credential = _credential(target.credential)
    config: Optional[dict[str, Any]] = None
    if target.kind == "argocd":
        server = _https(str(raw.get("server") or ""))
        app = str(raw.get("app") or "").strip()
        if not app or len(app) > 253:
            raise Refused("target_config", "Name the Argo CD app.")
        namespace = str(raw.get("app_namespace") or "").strip() or None
        config = {"server": server, "app": app, "app_namespace": namespace}
    elif target.kind == "webhook":
        config = {"url": _https(str(raw.get("url") or ""))}
        if credential is None:
            raise Refused("credential_required", "A webhook needs its signing secret.")
    elif target.kind == "script":
        run = str(raw.get("run") or "").strip()
        if not run or len(run) > RUN_MAX:
            raise Refused("target_config", "Give the script a command to run.")
        config = {"run": run}
    if target.kind in ("none", "forge_env"):
        credential = None
    return {"target_kind": target.kind, "target_config": config, "target_credential": credential}


def check_list(gates: Sequence[GateIn]) -> None:
    if len(gates) > GATES_MAX:
        raise Refused("gates_max", f"At most {GATES_MAX} checks in one list.")


def _cut(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summary(kind: str, config: Mapping[str, Any], forge: Optional[str] = None) -> str:
    """The one line every surface shows for a gate (AC1.8): what it checks, in plain words."""
    if kind == "command":
        return f"Runs {_cut(str(config.get('run') or ''))}"
    if kind == "ci":
        ci = CI_LABELS.get(forge or "", "CI")
        if config.get("all_checks"):
            return f"{ci} › all checks"
        how = "start a run" if config.get("mode") == "start" else "wait for the run"
        return f"{ci} › {_cut(str(config.get('workflow') or ''), 40)} · {how}"
    if kind == "webhook":
        # Host and path: two gates on one service (its /pass and /fail) must read apart.
        parts = urlsplit(str(config.get('url') or ''))
        if not parts.netloc:
            return "Asks a webhook"
        where = " ".join((parts.netloc + parts.path.rstrip("/")).split())
        # A long one keeps its end: the last segment is what tells two gates on one service apart.
        return f"Asks {where if len(where) <= 60 else '…' + where[-59:]}"
    if kind == "probe":
        return f"Checks {config.get('path') or '/'} answers {config.get('expect_min', 200)}–{config.get('expect_max', 399)}"
    if kind == "alerts":
        text = "No open alerts"
        if config.get("min_severity"):
            text += f" at {config['min_severity']} or above"
        if config.get("environment"):
            text += f" on {config['environment']}"
        return text
    if kind == "ticket":
        system = TICKET_LABELS.get(str(config.get("system")), "Ticket")
        names = " or ".join(str(s.get("name")) for s in config.get("states") or [])
        asked = " (asked when pressed)" if config.get("source") == "ask" else ""
        return _cut(f"{system} ticket is {names}{asked}", 80)
    return kind


def forge_label(forge: Optional[str]) -> Optional[str]:
    return FORGE_LABELS.get(forge or "")
