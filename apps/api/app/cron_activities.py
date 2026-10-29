"""The activity registry — what a cron can be pointed at, and the descriptor that says so.

A cron row names an `activity_type`; this is the single home for what those names mean. The REST layer
projects the descriptor (picker and params form), the runtime dispatches through it, and each activity
module registers into it. `load_activities()` imports the registering modules explicitly, so a
catalogue reader never drags them in. `invoke` is synchronous: the runtime runs each firing on a
dedicated single-thread executor to enforce a hard wall-clock timeout.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.crons import (
    ACTIVITY_RETURNABLE_OUTCOMES,
    RUN_OUTCOME_ACTED,
    RUN_OUTCOME_NOOP,
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
    SCHEDULE_KINDS,
)
from app.cron_schedule import cron_expression_for_interval, validate_cron_expression


class ActivityRegistryError(ValueError):
    """A descriptor (or a registration) that cannot be honoured. Raised at registration time, so a
    malformed descriptor fails at import rather than at the first firing."""


class UnknownActivityError(KeyError):
    """No descriptor is registered under that `activity_type`. Distinct from a malformed one: a row
    naming a retired activity is an operator-visible state, not a programming error."""


class ParamsSchemaError(ActivityRegistryError):
    """The descriptor's `params` is not a JSON Schema of the subset this registry accepts."""


class ParamsError(ValueError):
    """The params of one cron row do not satisfy its activity's schema. The message names the key."""


# ── The params schema subset ──────────────────────────────────────────────────────────────────────────

# A deliberate JSON Schema subset, not a dependency. Rule: a schema may not declare a constraint
# `validate_params` does not check (an unenforced `maxLength` writes unbounded strings into the
# ledger). Hence scalars plus an `array` of one scalar `items` spec; no `object`.
_SCHEMA_PROPERTY_KEYS = (
    # Enforced by `validate_params`.
    "type", "enum", "minimum", "maximum", "minLength", "maxLength",
    "items", "minItems", "maxItems", "uniqueItems",
    # Annotations: what the form renders, never a constraint.
    "title", "description", "default",
)

# The empty schema, spelled once so "no params" is one value.
NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

_JSON_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    # `bool` excluded from numerics: `True` is an `int` in Python.
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
}

# Derived from the checkers, so no declarable type lacks one (which would `KeyError` in validation).
_SCHEMA_PROPERTY_TYPES = tuple(_JSON_TYPE_CHECKS)
# What an array's `items` may be: one level deep, so a nested array is refused.
_SCHEMA_ITEM_TYPES = tuple(t for t in _SCHEMA_PROPERTY_TYPES if t != "array")

# Which bound applies to which type, so a misplaced bound is refused at declaration, not ignored.
_BOUNDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "string": ("minLength", "maxLength"),
    "integer": ("minimum", "maximum"),
    "number": ("minimum", "maximum"),
    "boolean": (),
    "array": ("items", "minItems", "maxItems", "uniqueItems"),
}
_ALL_BOUNDS = frozenset(b for bounds in _BOUNDS_BY_TYPE.values() for b in bounds)


def validate_params_schema(schema: Any) -> dict[str, Any]:
    """Return `schema` if it is an accepted params schema, else raise `ParamsSchemaError`.

    Called by `register`, so the failure lands on the author of the descriptor at import time.
    """
    if not isinstance(schema, dict):
        raise ParamsSchemaError(f"params schema must be an object, got {type(schema).__name__}")
    if schema.get("type") != "object":
        raise ParamsSchemaError("params schema must declare \"type\": \"object\"")
    if schema.get("additionalProperties") is not False:
        raise ParamsSchemaError(
            "params schema must declare \"additionalProperties\": false — an open schema turns a "
            "typo'd param into a silently ignored one"
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ParamsSchemaError("params schema must declare a \"properties\" object (possibly empty)")
    for name, spec in properties.items():
        if not isinstance(name, str) or not name:
            raise ParamsSchemaError(f"property name must be a non-empty string, got {name!r}")
        _validate_property_spec(repr(name), spec, _SCHEMA_PROPERTY_TYPES)
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise ParamsSchemaError("\"required\" must be a list of property names")
    undeclared = sorted(set(required) - set(properties))
    if undeclared:
        raise ParamsSchemaError(f"\"required\" names properties that are not declared: {undeclared}")
    unknown_top = sorted(set(schema) - {"type", "properties", "additionalProperties", "required",
                                        "title", "description"})
    if unknown_top:
        raise ParamsSchemaError(f"params schema carries unsupported keys {unknown_top}")
    return schema


def _validate_property_spec(label: str, spec: Any, types: tuple[str, ...]) -> None:
    """One property (or an array's `items`) spec against the subset; `label` names it in the error."""
    if not isinstance(spec, dict):
        raise ParamsSchemaError(f"property {label} must be an object")
    declared_type = spec.get("type")
    if declared_type not in types:
        raise ParamsSchemaError(f"property {label} must declare a \"type\" from {types}")
    unknown = sorted(set(spec) - set(_SCHEMA_PROPERTY_KEYS))
    if unknown:
        raise ParamsSchemaError(f"property {label} carries unsupported keys {unknown}")
    # A bound on a type that cannot carry it would be enforced by nothing.
    misplaced = sorted(set(spec) & _ALL_BOUNDS - set(_BOUNDS_BY_TYPE[declared_type]))
    if misplaced:
        raise ParamsSchemaError(f"property {label} is a {declared_type} and cannot carry {misplaced}")
    if declared_type != "array":
        return
    if "items" not in spec:
        raise ParamsSchemaError(f"property {label} is an array and must declare \"items\"")
    _validate_property_spec(f"{label}[items]", spec["items"], _SCHEMA_ITEM_TYPES)
    for bound in ("minItems", "maxItems"):
        value = spec.get(bound)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ParamsSchemaError(f"property {label}: {bound} must be a non-negative integer")
    if "uniqueItems" in spec and not isinstance(spec["uniqueItems"], bool):
        raise ParamsSchemaError(f"property {label}: uniqueItems must be a boolean")


def validate_params(descriptor: "ActivityDescriptor", params: Any) -> dict[str, Any]:
    """Check one row's `activity_params` against its activity's schema; return them, or raise
    `ParamsError` naming the key at fault.

    Single-homed here because the runtime validates too: a row predating a tightened schema fails at
    the wrapper with the form's message.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ParamsError(f"params must be an object, got {type(params).__name__}")
    schema = descriptor.params
    properties: dict[str, Any] = schema.get("properties", {})
    unknown = sorted(set(params) - set(properties))
    if unknown:
        raise ParamsError(
            f"activity {descriptor.type!r} accepts no parameter named {unknown[0]!r}"
            + (f" (also: {unknown[1:]})" if len(unknown) > 1 else "")
        )
    missing = sorted(name for name in schema.get("required", []) if name not in params)
    if missing:
        raise ParamsError(f"activity {descriptor.type!r} requires parameter {missing[0]!r}")
    for name, value in params.items():
        _check_one_param(descriptor, name, properties[name], value)
    return params


def _check_one_param(
    descriptor: "ActivityDescriptor", name: str, spec: dict[str, Any], value: Any
) -> None:
    """One parameter against its property spec. Every bound the subset admits is checked here;
    `_BOUNDS_BY_TYPE` keeps the two in step."""
    expected = spec["type"]
    if not _JSON_TYPE_CHECKS[expected](value):
        raise ParamsError(f"parameter {name!r} must be a {expected}, got {type(value).__name__}")
    choices = spec.get("enum")
    if choices is not None and value not in choices:
        raise ParamsError(f"parameter {name!r} must be one of {choices}, got {value!r}")
    if expected == "array":
        floor, ceiling = spec.get("minItems"), spec.get("maxItems")
        if floor is not None and len(value) < floor:
            raise ParamsError(f"parameter {name!r} must have at least {floor} item(s)")
        if ceiling is not None and len(value) > ceiling:
            raise ParamsError(f"parameter {name!r} has {len(value)} items; the limit is {ceiling}")
        for index, item in enumerate(value):
            _check_one_param(descriptor, f"{name}[{index}]", spec["items"], item)
        # Items are scalars of one type by now, so they hash, and `True`/`1` cannot mix.
        if spec.get("uniqueItems") and len(set(value)) != len(value):
            raise ParamsError(f"parameter {name!r} must not repeat an item")
        return
    if expected == "string":
        floor, ceiling = spec.get("minLength"), spec.get("maxLength")
        if floor is not None and len(value) < floor:
            raise ParamsError(f"parameter {name!r} must be at least {floor} characters")
        if ceiling is not None and len(value) > ceiling:
            raise ParamsError(
                f"parameter {name!r} is {len(value)} characters; the limit is {ceiling}"
            )
        return
    floor, ceiling = spec.get("minimum"), spec.get("maximum")
    if floor is not None and value < floor:
        raise ParamsError(f"parameter {name!r} must be at least {floor}")
    if ceiling is not None and value > ceiling:
        raise ParamsError(f"parameter {name!r} must be at most {ceiling}")


# ── What the wrapper hands an activity, and what it gets back ─────────────────────────────────────────


@dataclass(frozen=True)
class ActivityContext:
    """Everything one firing knows about itself.

    `session` is the wrapper's: the activity works inside that transaction and never opens its own, so
    a firing lands or does not. On the context so adding a fact is not a signature change.
    """

    cron_id: UUID
    cron_name: str
    run_id: UUID
    params: dict[str, Any]
    session: Session


@dataclass(frozen=True)
class ActivityResult:
    """One firing's answer: what it came to, and the evidence.

    `outcome` is `acted`/`noop` only; `skipped`/`missed`/`error` are the wrapper's own words. `task_id`
    links `cron_runs.task_id` to the row the activity created (only the activity knows it).
    """

    outcome: str
    detail: str = ""
    task_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.outcome not in ACTIVITY_RETURNABLE_OUTCOMES:
            raise ValueError(
                f"an activity may return only {ACTIVITY_RETURNABLE_OUTCOMES}, not {self.outcome!r} — "
                "skipped/missed/error are the runtime wrapper's to record"
            )

    @classmethod
    def acted(cls, detail: str = "", task_id: Optional[int] = None) -> "ActivityResult":
        return cls(outcome=RUN_OUTCOME_ACTED, detail=detail, task_id=task_id)

    @classmethod
    def noop(cls, detail: str = "") -> "ActivityResult":
        return cls(outcome=RUN_OUTCOME_NOOP, detail=detail)


@dataclass(frozen=True)
class DefaultSchedule:
    """The schedule a newly created cron for this activity starts on.

    Mirrors `ck_crons_exactly_one_schedule` (revision 0137), so an unstorable default fails at import.
    """

    kind: str
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind not in SCHEDULE_KINDS:
            raise ActivityRegistryError(f"default schedule kind must be one of {SCHEDULE_KINDS}")
        if self.kind == SCHEDULE_KIND_CRON:
            if not self.cron_expression or self.interval_seconds is not None:
                raise ActivityRegistryError(
                    "a 'cron' default schedule carries a cron_expression and no interval_seconds"
                )
            validate_cron_expression(self.cron_expression)
        else:
            if self.interval_seconds is None or self.cron_expression is not None:
                raise ActivityRegistryError(
                    "an 'interval' default schedule carries interval_seconds and no cron_expression"
                )
            # Raises `IntervalNotExpressibleError` at import rather than at reconcile time.
            cron_expression_for_interval(self.interval_seconds)


# ── The descriptor ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActivityDescriptor:
    """One thing a cron can be pointed at.

    `is_system` (stamped onto `crons.is_system`) and `creatable` (does the picker offer it) are
    independent: a creatable system activity is how a deleted one comes back.

    `timeout_seconds`/`min_gap_seconds` default the two overridable columns; None means the runtime's
    default, never zero.
    """

    type: str
    label: str
    description: str
    is_system: bool
    creatable: bool
    params: dict[str, Any]
    default_schedule: DefaultSchedule
    invoke: Callable[[datetime, ActivityContext], ActivityResult]
    timeout_seconds: Optional[float] = None
    min_gap_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.type or not self.type.strip():
            raise ActivityRegistryError("descriptor `type` must be a non-empty identifier")
        if not self.label or not self.label.strip():
            raise ActivityRegistryError(f"activity {self.type!r} must carry a label")
        if not self.description or not self.description.strip():
            raise ActivityRegistryError(f"activity {self.type!r} must carry a description")
        validate_params_schema(self.params)
        if not callable(self.invoke):
            raise ActivityRegistryError(f"activity {self.type!r}: `invoke` must be callable")
        for name in ("timeout_seconds", "min_gap_seconds"):
            value = getattr(self, name)
            if value is None:
                continue
            # `bool` excluded explicitly: `True` is an `int` and passes `> 0`.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ActivityRegistryError(
                    f"activity {self.type!r}: {name} must be a positive number or None — zero is not a "
                    "floor, it is the None case"
                )


# ── The registry ──────────────────────────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, ActivityDescriptor] = {}


def register(descriptor: ActivityDescriptor) -> ActivityDescriptor:
    """Add one descriptor. Refuses a duplicate `type`: otherwise last import wins silently."""
    if descriptor.type in _REGISTRY:
        raise ActivityRegistryError(f"activity {descriptor.type!r} is already registered")
    _REGISTRY[descriptor.type] = descriptor
    return descriptor


def load_activities() -> None:
    """Import every module that registers an activity. Idempotent (cached imports; `register` refuses
    duplicates).

    Imports live inside the function so importers of this module (e.g. the catalogue route) do not
    drag in every activity's dependencies.
    """
    # The reference descriptor below registers at this module's import, so the registry is never empty.
    from app import cron_activity_runner  # noqa: F401 — imported for its registration side effect
    from app import cron_retention  # noqa: F401 — imported for its `register` side effect
    from app import cron_system_activities  # noqa: F401 — imported for its `register` side effect


def get(activity_type: str) -> ActivityDescriptor:
    """The descriptor for `activity_type`, or `UnknownActivityError`. Loads the registry first."""
    load_activities()
    try:
        return _REGISTRY[activity_type]
    except KeyError:
        raise UnknownActivityError(
            f"no cron activity registered as {activity_type!r}; known: {sorted(_REGISTRY)}"
        ) from None


def all_descriptors() -> tuple[ActivityDescriptor, ...]:
    """Every registered descriptor, ordered by label — the picker's order, decided once here."""
    load_activities()
    return tuple(sorted(_REGISTRY.values(), key=lambda d: (d.label.lower(), d.type)))


def creatable_descriptors() -> tuple[ActivityDescriptor, ...]:
    """The subset the activity picker offers."""
    return tuple(d for d in all_descriptors() if d.creatable)


# ── The reference descriptor ──────────────────────────────────────────────────────────────────────────

# The reference descriptor: records a `noop` and touches nothing, making it the side-effect-free
# end-to-end probe of the cron pipeline. Creatable and not system, so an operator can run one on dev.


_NOOP_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "note": {
            "type": "string",
            "title": "Note",
            "description": "Recorded verbatim as the run's detail. Useful for telling two probes apart.",
            "maxLength": 200,
        },
    },
    "additionalProperties": False,
}


def _invoke_noop(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """Record that the schedule fired, and do nothing else.

    The detail carries `scheduled_at`, not `now()`, so it can be checked against the cron expression.
    """
    note = str(context.params.get("note") or "").strip()
    stamp = scheduled_at.isoformat()
    return ActivityResult.noop(detail=f"probe fired for {stamp}" + (f" — {note}" if note else ""))


NOOP_ACTIVITY = register(
    ActivityDescriptor(
        type="noop",
        label="No-op probe",
        description=(
            "Records a run and does nothing else. The end-to-end probe for the cron pipeline itself: "
            "if its runs appear on schedule, the engine, the wrapper and the ledger are all working."
        ),
        is_system=False,
        creatable=True,
        params=_NOOP_PARAMS,
        # Five minutes, so a forgotten probe is not the ledger's busiest writer.
        default_schedule=DefaultSchedule(kind=SCHEDULE_KIND_INTERVAL, interval_seconds=300),
        invoke=_invoke_noop,
        # It does no work; a slow firing means the runtime is misbehaving.
        timeout_seconds=30.0,
    )
)
