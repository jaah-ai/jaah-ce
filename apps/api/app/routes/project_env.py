"""The project Environment tab's five routes — the catalogue's only operator surface.

`app/models/projects.py::ProjectEnvVar` homes what a row means and `app/project_env.py` how one resolves; this
module owns the authority split between tiers and the warned-name list, and serializes everything else.

All five verbs are `projects:view` (R3/D10: anyone may set their own override), so the tier check lives
in `_authorize_tier`: a project default needs `projects:edit` on the project; an override is writable
only by its user (`projects:edit` does not lift it). Both refusals are 403, not 422.

The secret floor is structural: `EnvVarOut` has no field a resolved value could travel in, and
`_serialize` never calls `project_env.resolve_for_slot`. A row discloses `literal` (non-secret by
definition), the credential's name, and a per-row `status`. `status`/`winning` come off `resolve_rows`,
never recomputed; `resolve_rows` reads under `no_autoflush`, so writers flush first. A warned name saves
fine and warns; each sentence names the mechanism that wins (for `DATABASE_URL` and
`CREDENTIAL_ENCRYPTION_KEY`, `dc` unsets them, so the value takes effect).
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Literal, NamedTuple, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actors import stamp_actor
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import task_roster_select
from app.authz.visibility import has_project_key
from app.credential_crypto import KEY_ENV as _CREDENTIAL_ENCRYPTION_KEY_VAR
from app.credential_keys import LOCAL_KEY_ENV as _CREDENTIAL_LOCAL_PRIVATE_KEY_VAR
from app.credentials.registry import type_label
from app.db import get_session
from app.db_credentials import URL_VAR as _DATABASE_URL_VAR
from app.entity_shape import StrictModel, constraint_name, flush_or_409, get_or_404
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.projects import (
    PROJECT_ENV_VAR_NAME_RE,
    PROJECT_ENV_VAR_SOURCE_CREDENTIAL,
    PROJECT_ENV_VAR_SOURCE_LITERAL,
    Project,
    ProjectEnvVar,
)
from app.port_grid import range_starts
from app.project_identity import project_or_404
from app.project_env import RowStatus, resolve_rows
# `literal`/`description` are bound parameters, and psycopg's CHECK/NOT NULL text includes `Failing row
# contains (…)` even under `hide_parameters=True` — R10 forbids that reaching a log, so every write spans it.
from app.routes.credentials.credential_wire import _scrubbed_write
from app.user_activity import record_write

router = APIRouter(prefix="/api/projects", tags=["projects"])

_VIEW_KEY = permission_key("projects", "view")
_EDIT_KEY = permission_key("projects", "edit")

#: What a row may disclose about the credential it references — the whole of what is read, because
#: reading the model would read `config`. See `_credentials_by_id`.
class _CredentialRef(NamedTuple):
    name: str
    type: str


# ── The grammar, applied with `fullmatch` and never `match` ───────────────────────────────────────────
# Both patterns come from the schema; applied here so a malformed write is a readable 422, not an
# IntegrityError. `fullmatch` is a security requirement: `$` matches before a trailing newline, so
# `re.match` would admit `FOO\n`, injecting a second `.env` assignment.
_NAME_RX = re.compile(PROJECT_ENV_VAR_NAME_RE)
#: NUL, which no regex arm refuses: psycopg rejects it at the driver as a `DataError` (not an
#: `IntegrityError`), so refusing it here turns a 500 into a 422.
_NUL = "\x00"


def _is_one_line(value: str) -> bool:
    """Is `value` a single line with something on it — by Python's own definition of a line?

    Every node reader of a slot `.env` splits with `str.splitlines()` (`apps/jaahd/worktree_mechanics/`),
    which breaks on VT, FF, FS, GS, RS, NEL, LS and PS as well as CR/LF, and some of those readers scan
    other projects' slots — so a phantom assignment is a cross-tenant port hazard. Stricter than
    `ck_project_env_vars_literal` (safe; widening the CHECK is a follow-up). Spaces-only passes; empty
    does not."""
    return value != "" and _NUL not in value and value.splitlines() == [value]

# ── R2/D11: a raw secret literal is refused, and this route is where that happens ─────────────────────
# No CHECK can tell a hostname from a token (`models/projects.py::ProjectEnvVar`), and a `literal` is served in
# cleartext to every `projects:view` holder. A closed list of self-identifying shapes, no entropy
# heuristic (a false 422 blocks an operator) — a speed bump, not a proof. Left boundary
# `(?<![A-Za-z0-9])`, never `\b` (`_` is a word character). Second copy beside
# `apps/jaahd/transcript_redact.py` (another deployable). `re.search`, so embedded tokens are refused.
_SECRET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # `AKIA` (long-lived) and `ASIA` (STS) plus 16 upper/digit characters. The digit lookahead stops
    # an all-letter uppercase word run matching, at the cost of ~3.5% of real ids
    # ((26/32)^16) — accepted, since a false refusal is the worse error.
    (
        "an AWS access key id",
        re.compile(
            r"(?<![A-Za-z0-9])(?:AKIA|ASIA)(?=[0-9A-Z]{16}(?![0-9A-Za-z]))(?=[0-9A-Z]*[0-9])[0-9A-Z]{16}"
        ),
    ),
    # `ghp_`, `gho_`, `ghu_`/`ghs_`, `ghr_`, and the fine-grained PAT.
    ("a GitHub token", re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("a Slack token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}")),
    # This platform's own node credential (`node_credentials.py::TOKEN_PREFIX`) — the likeliest paste,
    # since operators set `NODE_TOKEN` on this tab.
    ("a jaah node token", re.compile(r"(?<![A-Za-z0-9])snt_[A-Za-z0-9_-]{16,}")),
    # Before the OpenAI arm, so the specific name wins.
    ("an Anthropic API key", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{16,}")),
    # Not a bare `sk-` prefix: with the hyphen inside the run it refused ordinary slugs like
    # `sk-prod-cluster-eu-west-1a`. Pinned to an unhyphenated base62 run, optionally behind one of the
    # three declared account prefixes.
    (
        "an OpenAI API key",
        re.compile(r"(?<![A-Za-z0-9])sk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{32,}|[A-Za-z0-9]{32,})"),
    ),
    ("a GitLab personal access token", re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{16,}")),
    ("a Tailscale auth key", re.compile(r"(?<![A-Za-z0-9])tskey-[A-Za-z0-9-]{10,}")),
    # The full `SG.<id>.<secret>` shape, not the bare `SG.` prefix.
    ("a SendGrid API key", re.compile(r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    # Trailing guard `(?![0-9A-Za-z_-])`, not `\b`: a key ending in `-` has no word boundary at end.
    ("a Google API key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    # `_is_one_line` refuses a whole PEM block, so what can arrive is the header; four dashes and a digit
    # in the type admit the SSH2 spelling.
    ("a PEM private key block", re.compile(r"-{4,5}\s?BEGIN[A-Z0-9 ]{0,32}PRIVATE KEY[A-Z0-9 ]{0,32}-{4,5}")),
)

#: Anything that cannot appear inside a JWT; splitting on it bounds `_is_a_jwt` to one linear pass.
_NOT_JWT_RX = re.compile(r"[^A-Za-z0-9_.-]+")


def _is_a_jwt(value: str) -> bool:
    """Does `value` carry a JWT — proven by decoding a header, never by the dotted shape alone?

    A segment must decode to a JSON object carrying `alg` or `typ` (RFC 7515), which no accidental
    dotted identifier does. A split plus a sliding window examines every consecutive triple, so a dotted
    prefix cannot misalign the scan (non-overlapping `finditer` missed `myservice.<jwt>`), and keeps it
    O(n) on a handler any `projects:view` holder can reach. Every decode failure is a `ValueError`
    subclass, so one arm catches them."""
    for chunk in _NOT_JWT_RX.split(value):
        parts = chunk.split(".")
        for index in range(len(parts) - 2):
            header = parts[index]
            if len(header) < 8 or len(parts[index + 1]) < 8 or len(parts[index + 2]) < 8:
                continue
            try:
                claims = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
            except ValueError:
                continue
            if isinstance(claims, dict) and ("alg" in claims or "typ" in claims):
                return True
    return False


def _secret_shape(value: str) -> Optional[str]:
    """The name of the shape `value` announces itself to be, or `None` — never the value."""
    for label, pattern in _SECRET_SHAPES:
        if pattern.search(value):
            return label
    return "a JSON Web Token" if _is_a_jwt(value) else None


#: The literal's ceiling — `routes/credentials/credential_wire.py::_VALUE_MAX_LEN`. This door is the only cap: the
#: columns are unbounded `Text` and there is no request-body ceiling.
_LITERAL_MAX_LEN = 4096


def _refuse_an_unstorable_literal(body: "EnvVarWriteIn") -> None:
    """Every rule that touches `literal`, handler-side — length, line shape, value/source pairing, and
    R2/D11's secret refusal — answered as an `HTTPException` that names no value.

    Not `field_validator`s: with no `RequestValidationError` handler, a pydantic 422 echoes the submitted
    value in its `input` key (a pasted PEM key came back verbatim), and validators run before this guard.
    Same rule as `routes/credentials/credential_wire.py::_validated_name`. Every write constructing an `EnvVarWriteIn`
    must reach this (`test_every_write_verb_calls_the_secret_literal_guard`), including
    `routes/agent_project_settings.py::_operator_write_body`. The message names the credential arm as
    the way forward."""
    if body.source == PROJECT_ENV_VAR_SOURCE_LITERAL:
        if body.literal is None:
            raise HTTPException(status_code=422, detail="source 'literal' requires a literal")
        if body.credential_id is not None or body.credential_field is not None:
            raise HTTPException(
                status_code=422, detail="source 'literal' takes no credential_id or credential_field"
            )
    else:
        if body.literal is not None:
            raise HTTPException(status_code=422, detail="source 'credential' takes no literal")
        if body.credential_id is None:
            raise HTTPException(status_code=422, detail="source 'credential' requires a credential_id")
        if body.credential_field is None:
            raise HTTPException(status_code=422, detail="source 'credential' requires a credential_field")

    literal = body.literal
    if literal is None:
        return
    if len(literal) > _LITERAL_MAX_LEN:
        raise HTTPException(
            status_code=422, detail=f"a literal must be at most {_LITERAL_MAX_LEN} characters"
        )
    if not _is_one_line(literal):
        raise HTTPException(
            status_code=422,
            detail=(
                "a literal must be a single non-empty line — an empty value writes VAR=, which looks set "
                "in the console and fails inside the container, and any line terminator would inject a "
                "second assignment into the env file a stack is started from"
            ),
        )
    shape = _secret_shape(literal)
    if shape is None:
        return
    raise HTTPException(
        status_code=422,
        detail=(
            f"this value looks like {shape}, and a raw secret literal is refused: a literal is stored "
            f"in cleartext and served in full to every holder of {_VIEW_KEY} on this project. Store the "
            "secret as a Credential — its config is encrypted at rest — and reference it here with "
            "source 'credential' plus the field that holds it. This request was recorded in the audit "
            "log before it was refused, so treat the value as exposed and rotate it."
        ),
    )


# ── The names that are written by something else, and what writes each ────────────────────────────────
# Each sentence names the mechanism that wins (§8.5); the port tiers are computed in `_warned_names`.
_WARN_COMPOSE_PROJECT_NAME = (
    "`apply_stack_identity` on the node writes COMPOSE_PROJECT_NAME on every claim, after this "
    "catalogue is applied, so a value set here is overwritten and never reaches the stack."
)
_WARN_NODE_ID = (
    "`apply_node_identity_env` on the node writes NODE_ID on every claim, after this catalogue is "
    "applied, so a value set here is overwritten and never reaches the stack."
)
# Not overwritten — that is the warning: the node's own credential lives in `/etc/jaah/node.env`
# (`jaah_cli._write_node_env`), and the DEV_ pair is never written into the real pair.
_WARN_NODE_CREDENTIAL = (
    "Nothing on the node applies this into a slot's .env — the node's own credential and identity come "
    "from /etc/jaah/node.env, and the DEV_ spelling is never written into NODE_ID/NODE_TOKEN. So a "
    "value set here is NOT overwritten: it takes effect inside the stack under a name the stack's own "
    "code reads as the node's identity."
)
# Imported, never typed: each has an owner (`db_credentials.URL_VAR`, `credential_crypto.KEY_ENV`), and
# `test_no_module_reads_database_url_directly` greps for a quoted `DATABASE_URL` literal.
_WARN_DATABASE_URL = (
    f"There is no applicator for {_DATABASE_URL_VAR} — `dc` deliberately `unset`s it (dc:35) so that a "
    f"per-command override still works. So a value set here is NOT overwritten: it takes effect and "
    f"points the stack at that database."
)
_WARN_CREDENTIAL_ENCRYPTION_KEY = (
    f"There is no applicator for {_CREDENTIAL_ENCRYPTION_KEY_VAR} — `dc` deliberately `unset`s it "
    f"(dc:39) so that a per-command override still works. So a value set here is NOT overwritten: it "
    f"takes effect and re-keys the stack's own credential store, making every credential it holds "
    f"unreadable."
)
_WARN_CREDENTIAL_LOCAL_PRIVATE_KEY = (
    f"There is no applicator for {_CREDENTIAL_LOCAL_PRIVATE_KEY_VAR} — `dc` deliberately `unset`s it "
    f"(dc:42) so that a per-command override still works. So a value set here is NOT overwritten: it "
    f"takes effect and re-keys the wrapping of every stored secret, making each of them unopenable."
)
_WARN_PORT_BAND = (
    "The port-band applicator (`apply_port_band`) assigns {name} from this project's grid band on every "
    "claim, after this catalogue is applied, so a value set here is overwritten and never reaches the "
    "stack."
)
_WARN_PORT_AUTO = (
    "The tier-2 auto-port re-inject (`apply_auto_ports`) assigns {name} on every claim, after this "
    "catalogue is applied, so a value set here is overwritten and never reaches the stack."
)
# A grid var this project does not declare is assigned by nothing, so the operator's value takes effect
# unchecked against the grid — warned like `DATABASE_URL`, not like a banded port.
_WARN_PORT_UNDECLARED = (
    "{name} is a tier-1 grid var for this org, but this project does not declare it in `port_vars` — so "
    "no applicator assigns it and a value set here is NOT overwritten. It takes effect, and nothing "
    "checks it against the node's port grid, so it can collide with another slot on the same host."
)

#: The fixed half of the warned-name list, name → sentence. The port halves are per project.
_FIXED_WARNINGS: dict[str, str] = {
    "COMPOSE_PROJECT_NAME": _WARN_COMPOSE_PROJECT_NAME,
    "NODE_ID": _WARN_NODE_ID,
    "NODE_TOKEN": _WARN_NODE_CREDENTIAL,
    "DEV_NODE_ID": _WARN_NODE_CREDENTIAL,
    "DEV_NODE_TOKEN": _WARN_NODE_CREDENTIAL,
    _DATABASE_URL_VAR: _WARN_DATABASE_URL,
    _CREDENTIAL_ENCRYPTION_KEY_VAR: _WARN_CREDENTIAL_ENCRYPTION_KEY,
    _CREDENTIAL_LOCAL_PRIVATE_KEY_VAR: _WARN_CREDENTIAL_LOCAL_PRIVATE_KEY,
}


def _warned_names(session: Session, project: Project) -> dict[str, str]:
    """`{NAME: the sentence naming what wins instead}` for one project.

    The port half is read live per org via `range_starts` (not `SEED_RANGE_STARTS`; not `grid()`, which
    can raise `GridUnavailable`). Tier 1 vs 2 is the band's complement, as
    `worktree_mechanics.ports.apply_auto_ports`. On a collision the fixed sentence wins (e.g. `DATABASE_URL`
    declared in `port_vars`)."""
    grid = set(range_starts(session, project.org_id))
    declared = list(project.port_vars or ())
    # The band is `fleet_decide.port_band`'s: the declared `port_vars` when any, else the whole grid — so
    # an undeclared grid var is banded by nothing.
    banded = (grid & set(declared)) if declared else grid
    warnings = {name: _WARN_PORT_BAND.format(name=name) for name in sorted(banded)}
    for name in sorted(grid - banded):
        warnings[name] = _WARN_PORT_UNDECLARED.format(name=name)
    for name in declared:
        if name in banded:
            continue
        warnings[name] = _WARN_PORT_AUTO.format(name=name)
    warnings.update(_FIXED_WARNINGS)
    return warnings


# ── The wire shape ────────────────────────────────────────────────────────────────────────────────────


class EnvVarCredentialOut(BaseModel):
    """The referenced credential, as much of it as R10 admits: a name, a type label and the field.

    No value field, ever. `id`, `name`, `type_label` are `None` on a tombstone (credential deleted,
    `credential_id` nulled); `field` survives, so the tab can say what a re-pointed credential needs."""

    id: Optional[UUID] = None
    name: Optional[str] = None
    type_label: Optional[str] = None
    field: str


class EnvVarOut(BaseModel):
    """One catalogue row — the single serialized shape every verb and the preview return."""

    id: UUID
    name: str
    user_id: Optional[UUID] = None
    scope: Literal["project", "user"]
    source: Literal["literal", "credential"]
    literal: Optional[str] = None
    credential: Optional[EnvVarCredentialOut] = None
    status: str
    status_detail: Optional[str] = None
    winning: bool
    warning: Optional[str] = None
    description: str = ""


class EnvVarWriteIn(StrictModel):
    """A create or an update.

    `user_id` is the tier: `None` is the project default, a value that user's override. The only
    accepted value is the caller's own — `_authorize_tier` refuses any other with 403."""

    # Capped because `_is_one_line` bounds shape, not size (`routes/credentials/credential_wire.py::_VALUE_MAX_LEN`).
    # `literal` carries no pydantic rule on purpose — a pydantic refusal echoes the value; its rules live
    # in `_refuse_an_unstorable_literal`.
    name: str
    user_id: Optional[UUID] = None
    source: Literal["literal", "credential"]
    literal: Optional[str] = None
    credential_id: Optional[UUID] = None
    credential_field: Optional[str] = Field(default=None, max_length=256)
    description: str = Field(default="", max_length=1024)

    @field_validator("name")
    @classmethod
    def _name_is_an_env_identifier(cls, value: str) -> str:
        # `fullmatch` also closes `_is_one_line`'s eight terminators and NUL — none is in `[A-Za-z0-9_]`.
        if not _NAME_RX.fullmatch(value):
            raise ValueError(
                "name must be an environment identifier: a letter or underscore followed by up to 127 "
                "letters, digits or underscores"
            )
        return value

    @field_validator("credential_field")
    @classmethod
    def _single_line_with_something_on_it(cls, value: Optional[str]) -> Optional[str]:
        """`credential_field` only — `literal` is checked in `_refuse_an_unstorable_literal`. A field is a
        schema key, never a value, so echoing it in a 422 discloses nothing."""
        if value is None:
            return None
        if not _is_one_line(value):
            raise ValueError(
                "must be a single non-empty line — an empty value writes VAR=, which looks set in the "
                "console and fails inside the container, and any line terminator would inject a second "
                "assignment into the env file a stack is started from"
            )
        return value

    @field_validator("description")
    @classmethod
    def _description_is_one_line_or_absent(cls, value: str) -> str:
        """Empty is legal (a label no stack reads), so not `_is_one_line`; NUL is refused because psycopg
        rejects the whole INSERT on one, which would be a 500."""
        if value == "":
            return value
        if not _is_one_line(value):
            raise ValueError("must be a single line and must not contain a NUL byte")
        return value

    # `ck_project_env_vars_shape`'s readable half lives in `_refuse_an_unstorable_literal`: a model
    # validator's error `input` is the whole body, `literal` included. Nothing on this model sees `literal`.

# ── Authority ─────────────────────────────────────────────────────────────────────────────────────────


def _authorize_tier(
    principal: Principal | None, actor: AdminUser, project_id: int, user_id: Optional[UUID]
) -> None:
    """The whole tier rule, for every write and every delete. 403 on refusal.

    Takes the tier of the row acted on (the stored row for PUT/DELETE, the new one for POST).
    `has_project_key`, not `principal.has`: a project default needs `projects:edit` on this project. It
    answers True for a superadmin and for a `None` principal; the agent plane
    (`routes/agent_project_settings.py`) never passes `None` — its `_launcher_authority` resolves the
    task's launcher or 403s.
    """
    if user_id is None:
        if not has_project_key(principal, _EDIT_KEY, project_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{_EDIT_KEY} on this project is required to write a project-level environment "
                    f"variable; any holder of {_VIEW_KEY} may set their own override instead"
                ),
            )
        return
    if user_id != actor.id:
        raise HTTPException(
            status_code=403,
            detail=(
                "an override belongs to the user it names — only that user may write or delete it, and "
                f"{_EDIT_KEY} does not lift that"
            ),
        )


#: The miss, worded once: `default_tier_row_or_404` must be indistinguishable from the catalogue lookups.
_NO_SUCH_ROW = "no such environment variable"


def default_tier_row_or_404(session: Session, project_id: int, env_var_id: UUID) -> ProjectEnvVar:
    """One project-default row of this project's catalogue, or 404 — the agent surface's lookup.

    An override gets the same 404, never a distinguishable refusal, so a runner cannot enumerate which
    users hold overrides."""
    row = get_or_404(session, ProjectEnvVar, env_var_id, detail=_NO_SUCH_ROW, where=(ProjectEnvVar.project_id == project_id,))
    if row.user_id is not None:
        raise HTTPException(status_code=404, detail=_NO_SUCH_ROW)
    return row


def _usable_credential_or_422(
    session: Session, principal: Principal | None, project: Project, credential_id: UUID, *,
    project_default: bool,
) -> _CredentialRef:
    """The referenced credential, refused with 422 unless the caller may actually resolve it.

    Uses `task_roster_select` on the project, the resolver's own select, so this and `app/project_env.py`'s
    `not_granted` status are one rule; both tiers resolve under the caller at write time (R3, applied to
    defaults too). 422 for both "no such" and "not yours", withholding the id space
    (`routes/node_groups.py::_validated_credential`'s convention). A `None` principal is refused — `authz`
    would read it as allow-all, and the agent plane reaches these writers without a feature guard.
    Refused, not asserted; no principal is built from the actor.

    A PROJECT DEFAULT may reference only an org credential (`owner_id IS NULL`): it resolves into every
    launcher's task, so a personal credential there would hand its owner's secret to everyone.

    Only the PROJECT's org's credentials: `project_env_vars.credential_id` is same-org keyed, so an
    operator naming its own org's credential on a tenant project gets this 422, not the key's 500."""
    if principal is None:
        raise HTTPException(
            status_code=403, detail="a credential reference needs a session principal to resolve under"
        )
    # Narrowed to columns: the entity would decrypt `config`, and before `_scrubbed_write` an
    # undecryptable row would raise an unscrubbed 500.
    usable = task_roster_select(principal, project.id).where(
        Credential.id == credential_id, Credential.org_id == project.org_id
    )
    if project_default:
        usable = usable.where(Credential.owner_id.is_(None))
    found = session.execute(
        usable.with_only_columns(Credential.id, Credential.name, Credential.type)
    ).first()
    if found is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "no such credential, or you cannot use it in this project — an environment variable may "
                "only reference a credential the identity it resolves under can use"
            ),
        )
    return _CredentialRef(name=found.name, type=found.type)


def _flush_or_conflict(session: Session) -> None:
    """Flush, translating this table's own constraint names into a status code: the two uniques are a 409
    (`flush_or_409`), its CHECKs a 422.

    Only the constraint name is matched, never the error text (hence the named constraints in
    `migrations/versions/202610251700_baseline.py`). Only `IntegrityError` is handled; everything else (including
    NUL's `DataError`) is re-raised for `_scrubbed_write`."""
    try:
        flush_or_409(
            session,
            "this project already has a variable by that name at that scope",
            constraints=frozenset({"uq_project_env_vars_default", "uq_project_env_vars_override"}),
        )
    except IntegrityError as exc:
        if (constraint_name(exc) or "").startswith("ck_project_env_vars_"):
            raise HTTPException(
                status_code=422, detail="the variable's name, value or shape is not admissible"
            ) from None
        raise


# ── Serialization ─────────────────────────────────────────────────────────────────────────────────────


def _credentials_by_id(session: Session, rows: list[ProjectEnvVar]) -> dict[UUID, _CredentialRef]:
    """The referenced credentials' name and type, in one query rather than one per row.

    A column select, never `select(Credential)`: loading the entity decrypts `config`, and one
    undecryptable credential would 500 the whole tab and every write (`app/project_env.py::prime`).
    Org-scoped by the seam but deliberately not grant-scoped: D20/R10 require every row to show the
    credential name and field. Discloses a name and type label only; bounded by this route's
    authorization."""
    ids = {row.credential_id for row in rows if row.credential_id is not None}
    if not ids:
        return {}
    return {
        row_id: _CredentialRef(name=name, type=type_)
        for row_id, name, type_ in session.execute(
            select(Credential.id, Credential.name, Credential.type).where(Credential.id.in_(ids))
        )
    }


def _serialize(
    row: ProjectEnvVar,
    *,
    statuses: dict[UUID, RowStatus],
    credentials: dict[UUID, _CredentialRef],
    warned: dict[str, str],
) -> EnvVarOut:
    """One row → one response. The one place that mapping happens.

    `statuses` has no default, so the resolver's answer is always supplied. `status` has five wire
    members: `ok`, the three `RowStatus` reasons, and `"unknown"` for a row absent from the map (unflushed
    by its caller) — unreachable from this module, present so a `KeyError` cannot 500 a write."""
    status = statuses.get(row.id)
    credential: Optional[EnvVarCredentialOut] = None
    if row.source == PROJECT_ENV_VAR_SOURCE_CREDENTIAL:
        held = credentials.get(row.credential_id) if row.credential_id is not None else None
        credential = EnvVarCredentialOut(
            # The id goes with the name. A NULL `credential_id` is a tombstone (its credential was deleted);
            # it is same-org keyed, so a set one is the project's org's. `held is None` beside a set id is a
            # credential `_credentials_by_id`'s org seam hid from this caller; serving the id would leak it.
            id=row.credential_id if held is not None else None,
            name=held.name if held is not None else None,
            type_label=type_label(held.type) if held is not None else None,
            field=row.credential_field or "",
        )
    return EnvVarOut(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        scope="project" if row.user_id is None else "user",
        source=row.source,  # type: ignore[arg-type]
        literal=row.literal,
        credential=credential,
        status=status.status if status is not None else "unknown",
        status_detail=status.detail if status is not None else None,
        winning=bool(status.winning) if status is not None else False,
        warning=warned.get(row.name),
        description=row.description or "",
    )


def _catalogue(session: Session, project_id: int) -> list[ProjectEnvVar]:
    """Every row of this project's catalogue — both tiers, every user's overrides included (D20).

    Ordered by name then tier, defaults first, so a default sits above its overrides."""
    return list(
        session.scalars(
            select(ProjectEnvVar)
            .where(ProjectEnvVar.project_id == project_id)
            .order_by(
                ProjectEnvVar.name,
                ProjectEnvVar.user_id.is_(None).desc(),
                ProjectEnvVar.user_id,
            )
        )
    )


def _rows_out(
    session: Session, project: Project, rows: list[ProjectEnvVar], viewer_id: Optional[UUID]
) -> list[EnvVarOut]:
    """`rows` serialized against one resolver pass and one warned-name computation.

    Write paths pay for the whole catalogue on purpose: `resolve_rows` should gain no single-row entry
    point (a third precedence path). Catalogues are a handful of rows."""
    statuses = resolve_rows(session, project.id, viewer_id)
    credentials = _credentials_by_id(session, rows)
    warned = _warned_names(session, project)
    return [
        _serialize(row, statuses=statuses, credentials=credentials, warned=warned) for row in rows
    ]


def default_tier_catalogue_out(session: Session, project: Project) -> list[EnvVarOut]:
    """This project's project-default rows, serialized — the agent surface's read, in `EnvVarOut`.

    Through `_rows_out`, so the wire shape stays single-homed. Overrides are omitted (the agent surface
    may not reach them). Takes no viewer: resolving against the launcher would report a correct default
    as `winning: false` beside an invisible override, and disclose that override. Filtered off
    `_catalogue` to keep one ordering rule."""
    rows = [row for row in _catalogue(session, project.id) if row.user_id is None]
    return _rows_out(session, project, rows, None)


def _activity_data(row: ProjectEnvVar, credential: Optional["_CredentialRef"]) -> dict:
    """What an activity entry says about a write — the variable, the scope, and the credential name and
    field. Never a value, `literal` included (R10/AC12(c)). After-state only; see `_changed_from`."""
    data: dict = {
        "project_id": row.project_id,
        "env_var_id": str(row.id),
        "name": row.name,
        "scope": "project" if row.user_id is None else "user",
        "source": row.source,
    }
    if row.user_id is not None:
        data["user_id"] = str(row.user_id)
    if row.source == PROJECT_ENV_VAR_SOURCE_CREDENTIAL:
        data["credential_name"] = credential.name if credential is not None else None
        data["credential_field"] = row.credential_field
    return data


def _changed_from(
    before_name: str, before_credential_id: Optional[UUID], row: ProjectEnvVar
) -> dict:
    """What an UPDATE moved, for the audit row — shape 2, the before/after pair.

    Read off the row before the assignments, since nothing else holds the old values; this is the shape
    `app/activity_kinds.py` claims for these sites. The credential is recorded as an id (joinable, no
    extra read). Empty when nothing moved."""
    changed: dict = {}
    if before_name != row.name:
        changed["name_from"] = before_name
    if before_credential_id != row.credential_id:
        changed["credential_from_id"] = (
            str(before_credential_id) if before_credential_id is not None else None
        )
    return changed


# ── The three writes, which are the same writes on both surfaces ──────────────────────────────────────
# The HTTP shells above own the lookup and the 404; everything a write decides is below.


#: `viewer_id`'s "not given" marker, distinct from an explicit `None`. Operator routes resolve `winning`
#: for the caller; the agent plane passes `None` (default tier only), so a runner never sees its
#: launcher's private override as a competitor — see `default_tier_catalogue_out`.
_CALLER_AS_VIEWER: Any = object()


def create_env_var(
    session: Session,
    project: Project,
    body: EnvVarWriteIn,
    actor: AdminUser,
    principal: Principal | None,
    *,
    viewer_id: Any = _CALLER_AS_VIEWER,
) -> EnvVarOut:
    """Insert one catalogue row, whole — the tier check, the literal rules, the credential-use check,
    the audit and the commit.

    Shared by `POST /api/projects/{id}/env-vars` and the agent `PUT /api/agent/tasks/{task_id}/project/env-vars`,
    so R2/D11 and R3 are unbypassable from either surface."""
    _authorize_tier(principal, actor, project.id, body.user_id)
    _refuse_an_unstorable_literal(body)
    credential = (
        _usable_credential_or_422(
            session, principal, project, body.credential_id, project_default=body.user_id is None
        )
        if body.credential_id is not None
        else None
    )
    row = ProjectEnvVar(
        name=body.name,
        description=body.description,
        project_id=project.id,
        user_id=body.user_id,
        source=body.source,
        literal=body.literal,
        credential_id=body.credential_id,
        credential_field=body.credential_field,
        org_id=project.org_id,
    )
    stamp_actor(row, actor, created=True)
    with _scrubbed_write(session, "create", "the environment variable"):
        session.add(row)
        _flush_or_conflict(session)
        # Serialized before the audit is staged, so `_rows_out`'s reads cannot autoflush it.
        out = _rows_out(
            session, project, [row],
            actor.id if viewer_id is _CALLER_AS_VIEWER else viewer_id,
        )[0]
        record_write(
            session,
            actor,
            "project_env_var_created",
            f"Added environment variable {row.name} to project {project.name}",
            data=_activity_data(row, credential),
        )
        session.commit()
    return out


def update_env_var(
    session: Session,
    project: Project,
    row: ProjectEnvVar,
    body: EnvVarWriteIn,
    actor: AdminUser,
    principal: Principal | None,
    *,
    viewer_id: Any = _CALLER_AS_VIEWER,
) -> EnvVarOut:
    """Edit one catalogue row in place, whole — same guards as the create, plus the tier-move refusal
    and the before/after half of the audit.

    Shared with the agent surface (see `create_env_var`). Authority is checked against the stored row,
    and a body naming a different tier is a 422 — moving tiers is a delete and a create.
    """
    _authorize_tier(principal, actor, project.id, row.user_id)
    if body.user_id != row.user_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "a variable's scope cannot be changed by an edit — delete it and create it at the "
                "other scope"
            ),
        )
    _refuse_an_unstorable_literal(body)
    credential = (
        _usable_credential_or_422(
            session, principal, project, body.credential_id, project_default=row.user_id is None
        )
        if body.credential_id is not None
        else None
    )
    # Read before the assignments — see `_changed_from`.
    before_name, before_credential_id = row.name, row.credential_id
    with _scrubbed_write(session, "update", "the environment variable"):
        row.name = body.name
        row.description = body.description
        row.source = body.source
        row.literal = body.literal
        row.credential_id = body.credential_id
        row.credential_field = body.credential_field
        stamp_actor(row, actor)
        _flush_or_conflict(session)
        out = _rows_out(
            session, project, [row],
            actor.id if viewer_id is _CALLER_AS_VIEWER else viewer_id,
        )[0]  # before the audit — see `create_env_var`
        record_write(
            session,
            actor,
            "project_env_var_updated",
            f"Updated environment variable {row.name} on project {project.name}",
            data={
                **_activity_data(row, credential),
                **_changed_from(before_name, before_credential_id, row),
            },
        )
        session.commit()
    return out


def delete_env_var(
    session: Session,
    project: Project,
    row: ProjectEnvVar,
    actor: AdminUser,
    principal: Principal | None,
) -> None:
    """Remove one catalogue row, whole — the tier check, the pre-delete audit snapshot and the commit.

    Shared by the operator DELETE and the agent `DELETE /api/agent/tasks/{task_id}/project/env-vars`, so
    `_authorize_tier` and the snapshot are identical on both."""
    _authorize_tier(principal, actor, project.id, row.user_id)
    # Through `_credentials_by_id`, not `session.get` (seam), and the same read the serializer makes.
    credential = _credentials_by_id(session, [row]).get(row.credential_id)
    data = _activity_data(row, credential)
    name = row.name
    with _scrubbed_write(session, "delete", "the environment variable"):
        session.delete(row)
        record_write(
            session,
            actor,
            "project_env_var_deleted",
            f"Removed environment variable {name} from project {project.name}",
            data=data,
        )
        session.commit()


# ── The five routes ───────────────────────────────────────────────────────────────────────────────────


@router.get("/{project_id}/env-vars", response_model=list[EnvVarOut])
@feature("projects", "view", object_type="project", object_params=("project_id",))
def list_project_env_vars(
    project_id: int,
    actor: AdminUser = Depends(require_user),
    _principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[EnvVarOut]:
    """The whole catalogue for this project, at both tiers.

    Every user's overrides (D20 relaxed visibility; authority stays in `_authorize_tier`). `winning` is
    resolved for the caller."""
    project = project_or_404(session, project_id, "no such project")
    return _rows_out(session, project, _catalogue(session, project_id), actor.id)


@router.get("/{project_id}/env-vars/preview", response_model=list[EnvVarOut])
@feature("projects", "view", object_type="project", object_params=("project_id",))
def preview_project_env_vars(
    project_id: int,
    user_id: Optional[UUID] = Query(default=None),
    actor: AdminUser = Depends(require_user),
    _principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[EnvVarOut]:
    """What this project's catalogue contributes to a slot resolved for `user_id` — the CP-managed set
    and nothing else.

    The `winning` rows of `resolve_rows(project_id, user_id)`; never calls `resolve_for_slot`, so no
    value is returned. A winner whose `status` is not `ok` will not be written. `user_id` defaults to the
    caller; any other id is admitted without a 404 (D20 already serves every override, and a 404 would
    probe the user id space)."""
    project = project_or_404(session, project_id, "no such project")
    viewer_id = user_id if user_id is not None else actor.id
    statuses = resolve_rows(session, project_id, viewer_id)
    rows = [
        row
        for row in _catalogue(session, project_id)
        if (status := statuses.get(row.id)) is not None and status.winning
    ]
    credentials = _credentials_by_id(session, rows)
    warned = _warned_names(session, project)
    return [
        _serialize(row, statuses=statuses, credentials=credentials, warned=warned) for row in rows
    ]


@router.post("/{project_id}/env-vars", response_model=EnvVarOut, status_code=201)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def create_project_env_var(
    project_id: int,
    body: EnvVarWriteIn,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> EnvVarOut:
    """Declare a variable — a project default (needs `projects:edit`) or the caller's own override.

    The HTTP shell: lookup, 404, and the shared `create_env_var`."""
    project = project_or_404(session, project_id, "no such project")
    return create_env_var(session, project, body, actor, principal)


@router.put("/{project_id}/env-vars/{env_var_id}", response_model=EnvVarOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def update_project_env_var(
    project_id: int,
    env_var_id: UUID,
    body: EnvVarWriteIn,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> EnvVarOut:
    """Edit a variable in place.

    The HTTP shell: lookups, 404s, and the shared `update_env_var`."""
    project = project_or_404(session, project_id, "no such project")
    row = get_or_404(session, ProjectEnvVar, env_var_id, detail=_NO_SUCH_ROW, where=(ProjectEnvVar.project_id == project_id,))
    return update_env_var(session, project, row, body, actor, principal)


@router.delete("/{project_id}/env-vars/{env_var_id}", status_code=204)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def delete_project_env_var(
    project_id: int,
    env_var_id: UUID,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Remove a variable. Same authority as the write: a project default needs `projects:edit`, an
    override is its own user's. The HTTP shell over the shared `delete_env_var`; answers 204."""
    project = project_or_404(session, project_id, "no such project")
    row = get_or_404(session, ProjectEnvVar, env_var_id, detail=_NO_SUCH_ROW, where=(ProjectEnvVar.project_id == project_id,))
    delete_env_var(session, project, row, actor, principal)
    return Response(status_code=204)
