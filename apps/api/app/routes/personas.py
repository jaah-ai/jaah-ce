"""The persona roster — a read-only projection of the registry that decides what a persona is.

Answers, for Automation: what is a persona, what model does it run, what does it owe before it
may settle, how may it end, and what is its role brief.

It owns no fact: every string is read at request time from its home, or is a mechanical derivation of a
key. Sources: `app/persona_registry.py` (one record per persona — every persona fact);
`app/dispatch_compose/roles.py::_ROLES` (role prose, keyed by charter key — not injective, `coder` runs
`implementer`) and its `_NEVER_ASKS`/`_CONSOLE_INTERVIEW`/`_NO_AUTOCLOSE` carve-outs;
`app/task_manifest.py::ARTIFACT_KIND_LABELS`/`KIND_MEDIA`; `app/task_settle.py::TERMINAL_REFERENCES`.

The composed prompt is deliberately absent: the universal charter is identical for every persona and
would bury the differences, and composing it reads doctrine off disk.

A missing row is a null, never a 500: tables other than the registry are separate hand-maintained key
spaces, read with `.get()`.

Authorization is `tasks:create`, held anywhere: whoever may file a task may read what each persona it
could be dispatched as does — Project Developer and up, never a Project Viewer. The subject is a code
constant, identical for every org, so the org-scoped plane reads no tenant row here.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import agent_providers
from app.auth import require_user
from app.authz.declarations import feature
from app.dispatch_compose.role_tables import _CONSOLE_INTERVIEW, _NEVER_ASKS, _NO_AUTOCLOSE
from app.dispatch_compose.roles import _ROLES
from app.dispatch_compose.system_prompt import apply_provider_values, resolve_provider
from app.models.identity import AdminUser
from app.persona_registry import PERSONA_CHARTER, REGISTRY
from app.task_manifest import (
    ARTIFACT_KIND_LABELS,
    KIND_MEDIA,
    REQUIRED_HARD,
)
from app.task_settle import TERMINAL_REFERENCES

router = APIRouter(prefix="/api/personas", tags=["personas"])


# ── Which `_ROLES` keys are prose, and in what order ──────────────────────────────────────────────────
# `title` is the role's name (the roster answers with its own); everything else is prose. The order is
# the order a role brief reads in, not the dict's — `_ROLES` is not consistent between roles, and dict
# order would give two personas' pages two section orders.
_ROLE_SECTION_ORDER: tuple[str, ...] = (
    "focus",
    "inputs",
    "input_slots",
    "skills",
    "tools",
    "gate_name",
    "gate",
    "output_contract",
    "tool",
    "extra_rules",
    "settle_summary_hint",
)

# `_ROLES` keys deliberately not rendered as sections (the fallback renders every unrecognised key, so an
# exclusion must be a decision):
#   · `title` — the role's name; differs from the persona's (`coder` runs `implementer`).
#   · `no_terminal_call` — a bool, already visible as an empty `terminal_types`.
#   · `structured_endpoint`, `tool_heading`, `settle_gate_arg` — substitution fragments spliced into
#     sentences `dispatch_compose` composes elsewhere; misleading on their own.
_ROLE_SECTIONS_EXCLUDED: frozenset[str] = frozenset({
    "title",
    "no_terminal_call",
    "structured_endpoint",
    "tool_heading",
    "settle_gate_arg",
})

# ── Wire models ───────────────────────────────────────────────────────────────────────────────────────


class PersonaRow(BaseModel):
    """One persona as the roster lists it — identity, dispatch knobs, and one line of role focus.

    Every field is a projection; `Persona` carries the reasoning.
    """

    #: `Persona.name` — the value that lands in `tasks.persona`.
    name: str
    #: The persona's name for this page, a mechanical title-casing of the key (`_humanize`). Not
    #: `Persona.label` (the board's verb for a task row), and not `option_label` (`None` for `plan`,
    #: `finish` and the system personas); the title-case covers every row.
    label: str
    #: `Persona.charter_key` — which `_ROLES` entry this persona runs; not injective, so a distinct field.
    charter_key: str
    #: `Persona.model` — the stage default, one rung of `model_catalog.MODEL_PRECEDENCE`; a Task's own
    #: pin outranks it.
    default_model: str
    #: `Persona.mode` — which flow of the universal charter governs the role's finish.
    mode: Optional[str]
    #: `Persona.delivery` — the role's RESULT channel, off the record for `mode`'s reason.
    delivery: Optional[str]
    #: `Persona.done_criterion` — what moves the row to `done`. Null only on `epic`, not in this roster.
    done_criterion: Optional[str]
    #: `Persona.dispatchable` — true for every row here by construction (the roster is `PERSONA_CHARTER`);
    #: read rather than a literal so it stays honest if the roster's source widens.
    dispatchable: bool
    #: `Persona.operator_pickable` — whether an operator may pick it on a top-level Task.
    operator_selectable: bool
    #: `not Persona.pipeline` — the hidden, agent-created population; never true with
    #: `operator_selectable`.
    system_persona: bool
    #: `Persona.terminals` — the endings this persona may declare. Empty is a declared answer
    #: (`done_criterion == "operator"`).
    terminal_types: list[str]
    #: `_ROLES[charter_key]["focus"]` through `_baseline_prose`, trimmed — the roster subtitle, plain text,
    #: rendered by the same pass as `RoleSectionOut.body`.
    focus: Optional[str]


class PersonasOut(BaseModel):
    """The whole roster, in the registry's own declaration order — carried, not sorted, because
    `app/persona_registry.py` calls that order load-bearing.
    """

    personas: list[PersonaRow]


class ManifestRowOut(BaseModel):
    """One deliverable a persona owes before it may settle — a `Persona.manifest` row, rendered."""

    #: The artifact kind (`task_manifest.ARTIFACT_KINDS`).
    kind: str
    #: `ARTIFACT_KIND_LABELS[kind]`, read with `.get()` so a miss is visible, not fatal.
    label: Optional[str]
    #: `KIND_MEDIA[kind][0]` — the extensions that satisfy this kind; empty means "any file".
    extensions: list[str]
    #: True for `REQUIRED_HARD` — the settle is refused without this deliverable.
    required: bool
    #: The manifest's own word (`hard` / `advisory`): advisory is recorded-then-proceeds, not optional, so
    #: the bool alone loses the distinction.
    level: str


class TerminalOut(BaseModel):
    """One ending a persona may declare, with what that ending must carry."""

    #: A `task_manifest.TERMINAL_TYPES` member.
    type: str
    #: `task_settle.TERMINAL_REFERENCES[type]` — the settle parameters this ending requires; often empty.
    required_references: list[str]


class TaskCreationLicenceOut(BaseModel):
    """The persona's authority to file work — a `persona_registry.TaskCreationLicence`, rendered.

    Null on a persona holding no licence (nearly all; R30 makes creation a named licence).
    """

    #: The licence's `shapes`, sorted — a `frozenset`'s order is not stable across processes.
    shapes: list[str]
    #: The licence's `constraint` verbatim — the sentence the server quotes when it refuses a call.
    constraint: str


class RoleSectionOut(BaseModel):
    """One prose section of the role brief, exactly as `_ROLES` holds it."""

    #: The `_ROLES` key.
    key: str
    #: The `_ROLES` key, title-cased.
    title: str
    #: The text the runner is handed, as plain text (markdown-shaped, but not promised). Verbatim except
    #: that provider conditionals are resolved to the baseline reading — see `_baseline_prose`.
    body: str


class PersonaDetailOut(PersonaRow):
    """One persona's whole page. Extends the roster row so one producer builds the shared half
    (`app/routes/workflows.py`'s reason), plus the single-persona facts.
    """

    #: `Persona.settle_states` — the states a runner may settle its own row from (that axis only).
    settle_states: list[str]
    #: Whether this role's gate may ask — `charter_key not in dispatch_compose.role_tables._NEVER_ASKS`.
    interviews: bool
    #: Whether the operator is in the room — `charter_key in _CONSOLE_INTERVIEW`. It overrides the
    #: interview machinery, so it is separate from `interviews`.
    console_interview: bool
    #: Whether the runner may be reaped as idle — `charter_key not in _NO_AUTOCLOSE`.
    autoclose: bool
    #: `Persona.autonomous_default` — the `autonomous` a row is born with. Null means undeclared
    #: (`routes/tasks/policy.py::create_autonomous_for_persona` resolves it to `True`); kept null so a client can
    #: tell declared from inherited.
    autonomous_default: Optional[bool]
    #: `Persona.manifest`, in declaration order — the order every refusal renders a gap in.
    manifest: list[ManifestRowOut]
    #: `Persona.terminals` joined to `TERMINAL_REFERENCES`, so the join lives in one place.
    terminals: list[TerminalOut]
    #: `Persona.licence`, or null where the persona holds none.
    task_creation_licence: Optional[TaskCreationLicenceOut]
    #: The role brief from `_ROLES[charter_key]`, ordered by `_ROLE_SECTION_ORDER`. An uncarried key is
    #: absent, never an empty section. Empty for a persona with no `_ROLES` entry.
    role_sections: list[RoleSectionOut]


# ── Derivations ───────────────────────────────────────────────────────────────────────────────────────


def _humanize(key: str) -> str:
    """A snake_case `_ROLES` key as a section title reads. `str.title()` is imperfect on acronyms;
    accepted over a curated title table this module would have to keep true.
    """
    return key.replace("_", " ").title()


def _role(charter_key: str) -> dict[str, Any]:
    """The `_ROLES` entry for a charter key, or an empty dict.

    `.get()`: a charter registered before its role must render a persona with no brief, not fail the
    roster.
    """
    return _ROLES.get(charter_key) or {}


def _baseline_prose(body: str) -> str:
    """One `_ROLES` body as the baseline (default provider) reading.

    `_ROLES` bodies are templates carrying `dispatch_compose`'s `<!--if:F-->…<!--else:F-->…<!--end:F-->`
    blocks; unresolved they would render both contradictory arms as plain text. A persona has no provider
    or row here, so the baseline (`DEFAULT_PROVIDER`, `epic=True`, the current names) is the reading true of the
    traffic this page describes.

    The raise is swallowed per body and the raw body rendered: `_ROLES` is developer source, and the
    composer's full-tier test may not run on an `apps/` diff (quick tier), so this is a live fallback.
    The marker punctuation on screen is the evidence of the authoring mistake; one bad body costs only
    its own section.
    """
    try:
        return apply_provider_values(
            body, resolve_provider(agent_providers.DEFAULT_PROVIDER), epic=True, repo=True)
    except ValueError:
        return body


def _role_sections(charter_key: str) -> list[RoleSectionOut]:
    """The role's prose sections, ordered.

    First `_ROLE_SECTION_ORDER`, then every remaining string key in declaration order — so a new `_ROLES`
    key shows up rather than being silently dropped; true exclusions go in `_ROLE_SECTIONS_EXCLUDED`.
    Strings only (`no_terminal_call` is a bool). Every body goes through `_baseline_prose`.
    """
    role = _role(charter_key)
    rendered: list[RoleSectionOut] = []

    def _section(key: str, body: object) -> None:
        """Append one section, resolved first and judged empty second: a body that is only a block the
        baseline drops is empty as prose."""
        if not isinstance(body, str) or not body.strip():
            return
        resolved = _baseline_prose(body)
        if not resolved.strip():
            return
        rendered.append(RoleSectionOut(key=key, title=_humanize(key), body=resolved))

    for key in _ROLE_SECTION_ORDER:
        _section(key, role.get(key))
    for key, body in role.items():
        if key in _ROLE_SECTION_ORDER or key in _ROLE_SECTIONS_EXCLUDED:
            continue
        _section(key, body)
    return rendered


def _row_out(name: str) -> dict[str, Any]:
    """The roster fields as a dict, so the list row and the detail are built by one producer
    (`app/routes/workflows.py::_type_out`'s reason). Every fact comes off `REGISTRY[name]`; only `focus`
    is a join (prose is keyed by charter key).
    """
    p = REGISTRY[name]
    role = _role(p.charter_key)
    # Through `_baseline_prose` like every `_ROLES` body, so the subtitle and the detail page cannot
    # render one string two ways. Resolve first, strip second, judge emptiness on the resolved text.
    focus = role.get("focus")
    focus = _baseline_prose(focus).strip() if isinstance(focus, str) else ""
    return {
        "name": p.name,
        # `_humanize(name)`, not `p.label` — see `PersonaRow.label`.
        "label": _humanize(name),
        "charter_key": p.charter_key,
        "default_model": p.model,
        # Off the record: both knobs are persona properties.
        "mode": p.mode,
        "delivery": p.delivery,
        "done_criterion": p.done_criterion,
        "dispatchable": p.dispatchable,
        "operator_selectable": p.operator_pickable,
        "system_persona": not p.pipeline,
        "terminal_types": list(p.terminals),
        "focus": focus or None,
    }


def _detail_out(name: str) -> PersonaDetailOut:
    """One persona's whole page: the record, plus the tables it joins to (read with `.get()`)."""
    p = REGISTRY[name]
    licence = p.licence
    return PersonaDetailOut(
        **_row_out(name),
        settle_states=list(p.settle_states),
        interviews=p.charter_key not in _NEVER_ASKS,
        console_interview=p.charter_key in _CONSOLE_INTERVIEW,
        autoclose=p.charter_key not in _NO_AUTOCLOSE,
        autonomous_default=p.autonomous_default,
        manifest=[
            ManifestRowOut(
                kind=kind,
                label=ARTIFACT_KIND_LABELS.get(kind),
                # `KIND_MEDIA` holds only format-pinned kinds, so a miss means no extensions.
                extensions=list(KIND_MEDIA.get(kind, ((), ""))[0]),
                required=level == REQUIRED_HARD,
                level=level,
            )
            for kind, level in p.manifest
        ],
        terminals=[
            TerminalOut(type=terminal, required_references=list(TERMINAL_REFERENCES.get(terminal, ())))
            for terminal in p.terminals
        ],
        task_creation_licence=(
            None
            if licence is None
            else TaskCreationLicenceOut(
                shapes=sorted(licence.shapes), constraint=licence.constraint
            )
        ),
        role_sections=_role_sections(p.charter_key),
    )


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=PersonasOut)
@feature("tasks", "create")
def list_personas(
    _actor: AdminUser = Depends(require_user),
) -> PersonasOut:
    """Every dispatchable persona (`PERSONA_CHARTER`), in the registry's declaration order.

    `epic` is a container and `janitor`/`harvest`/`triage` hold no charter, so they have no brief to show.
    No `session` parameter: every fact is a compiled-in table, identical on every replica.
    """
    return PersonasOut(personas=[PersonaRow(**_row_out(name)) for name in PERSONA_CHARTER])


@router.get("/{name}", response_model=PersonaDetailOut)
@feature("tasks", "create")
def get_persona(
    name: str,
    _actor: AdminUser = Depends(require_user),
) -> PersonaDetailOut:
    """Everything the control plane knows about one persona.

    The 404 names the dispatchable vocabulary, because the common miss is a real persona with no
    charter (`epic`, `janitor`, `harvest`, `triage`), which "unknown persona" alone would misread.
    """
    persona_row = REGISTRY.get(name)
    # The charter roster, not `dispatchable`: a script persona (`merge_lane`) dispatches but has no charter
    # for this endpoint to describe.
    if persona_row is None or name not in PERSONA_CHARTER:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown persona {name!r}; this endpoint describes the DISPATCHABLE personas "
                f"(app/persona_registry.py, the rows holding a charter): {', '.join(PERSONA_CHARTER)}"
            ),
        )
    return _detail_out(name)
