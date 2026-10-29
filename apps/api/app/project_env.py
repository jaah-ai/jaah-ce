"""Resolving a project's environment variables — the single home for the resolution rule.

`resolve_for_slot` (one winner per name) and `resolve_rows` (a status per row, for the Environment
tab) share one resolver so the two cannot disagree. Rules: R4 `user override > project default`; D7
identity per `_identity_of`; R9 an unresolved reference omits the line, never `VAR=` (`_usable`); R10
no resolved value is returned, logged or recorded, and `detail` never names the credential.

Every read runs under `no_autoflush`: a caller that writes then resolves must flush in between. The
caller supplies the lease identity (`None` = no override applies). The omission reasons are closed;
an unexpected failure is withheld as `not_granted`. Cross-org refusal is the read seam's.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import task_roster_select
from app.credential_crypto import SealedSecret, open_secret
from app.credential_keys import is_nodevault_key_id
from app.credentials.registry import secret_keys
from app.tenancy.seam import TenancyScopeConflict
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.projects import PROJECT_ENV_VAR_NAME_RE, PROJECT_ENV_VAR_SOURCE_CREDENTIAL, ProjectEnvVar

log = logging.getLogger(__name__)

#: A row that resolved. `RowStatus.status` is this or one of the three reasons below.
STATUS_OK = "ok"

#: `source='credential'` with `credential_id IS NULL` — the tombstone a credential delete leaves.
REASON_CREDENTIAL_MISSING = "credential_missing"
#: The resolving identity cannot use the credential in this project (including the system actor, which
#: can use none). The value predates bindings and is kept: it is a wire status.
REASON_NOT_GRANTED = "not_granted"
#: `credential_field` is not a key of the credential's stored `config`, holds no usable value, or is
#: a sealed secret that could not be opened (a moved, re-pointed or tampered envelope).
REASON_FIELD_ABSENT = "field_absent"

#: `credential_field` is a secret sealed under the org's NodeVault key, which only its nodes open (AC-5.4):
#: unavailable to the control plane, so the variable is omitted rather than resolved here.
REASON_NODEVAULT = "nodevault"

#: The whole vocabulary; consumers render from this tuple.
OMISSION_REASONS: tuple[str, ...] = (
    REASON_CREDENTIAL_MISSING,
    REASON_NOT_GRANTED,
    REASON_FIELD_ABSENT,
    REASON_NODEVAULT,
)


#: `ck_project_env_vars_name`'s grammar, taken from the model so the CHECK and this floor are one rule.
_NAME_RE = re.compile(PROJECT_ENV_VAR_NAME_RE)


def _usable(value: Any) -> bool:
    """Whether a credential field's stored value may become an environment variable's value.

    The rule the schema puts on a literal (`ck_project_env_vars_literal`): a non-empty `str` free of
    CR and LF; anything else takes `field_absent`. Blank-but-not-empty (`'   '`) passes, as in the
    CHECK. `''` is reachable (non-secret fields store it verbatim) and would write `VAR=`. Non-`str` is
    refused, not coerced (a repr could leak sibling fields). CR/LF is a security control — a newline
    injects further assignments — refused here so the tab and the slot agree; `${VAR}` interpolation is
    closed by the renderer's single-quoting (`app/models/projects.py::ProjectEnvVar`)."""
    return isinstance(value, str) and value != "" and "\n" not in value and "\r" not in value


@dataclass(frozen=True, repr=False)
class SlotEnv:
    """What ONE slot's environment resolves to — the WINNERS only, one entry per distinct name.

    `values` and `omit` are disjoint; an omitted name is absent from `values`, never empty (R9).
    `omit` carries names only — reasons belong to rows (`resolve_rows`).
    """

    values: dict[str, str]
    omit: list[str]

    def __repr__(self) -> str:
        """Redacted, since `values` holds secrets. Not complete: `asdict`, `astuple` and `__eq__` still
        reach the fields."""
        return f"SlotEnv(values=<{len(self.values)} redacted>, omit={self.omit!r})"


@dataclass(frozen=True)
class RowStatus:
    """How ONE catalogue row resolves, independently of whether it is the one a slot would get.

    `status` is `STATUS_OK` or one of `OMISSION_REASONS`; `detail` never carries a value or the
    credential's name. `winning` is True for the row the viewer's slot would receive (same R4
    comparison); a row can be winning and omitted at once.
    """

    status: str
    detail: Optional[str]
    winning: bool


@dataclass(frozen=True, repr=False)
class _Resolved:
    """One row's resolution, before it is projected into either public shape.

    `status` is `STATUS_OK` only when `value` is usable (`_usable`), classified once here so both entry
    points read the same status."""

    status: str
    detail: Optional[str]
    # Not Optional: an omission has no value and an `ok` always has a usable one.
    value: str = ""

    def __post_init__(self) -> None:
        """Enforce the invariant, since the default makes a `VAR=` `ok` one forgotten argument away.
        Raising inside `resolve`'s guard withholds the variable (fail closed)."""
        if self.status == STATUS_OK and not _usable(self.value):
            raise ValueError("a resolved value must be usable; see `_usable`")

    def __repr__(self) -> str:
        """Redacted for `SlotEnv.__repr__`'s reason, and the same incompleteness applies."""
        return f"_Resolved(status={self.status!r}, detail={self.detail!r}, value=<redacted>)"


def resolve_for_slot(
    session: Session, project_id: int, user_id: Optional[UUID]
) -> SlotEnv:
    """The environment `project_id`'s slot gets when it is leased for `user_id`.

    `user_id` is the identity the lease is for; `None` means no per-user override applies.

    Never raises over a bad row: a broken row lands in `omit` rather than failing the claim.
    `SQLAlchemyError` and `TenancyScopeConflict` propagate by design (the session is the caller's; two
    deciding scopes is a defect)."""
    resolver = _Resolver(session, project_id)
    winners = _winners(_rows(session, project_id), user_id)
    # Primed on the winners only: each identity costs several queries and other users' overrides are
    # invisible here (priming them would also decrypt their credentials needlessly).
    resolver.prime(winners.values())
    values: dict[str, str] = {}
    omit: list[str] = []
    # Sorted, so the same catalogue always renders the same env text (the node overlay hashes it).
    for name in sorted(winners):
        resolved = resolver.resolve(winners[name])
        # Status alone, as in `resolve_rows`. The name is floored here, the only place a name becomes
        # an env-file line: an unflushed edit may bypass `ck_project_env_vars_name`. `fullmatch`, never
        # `match` — Python's `$` accepts a trailing newline, which is the injection this blocks.
        if resolved.status == STATUS_OK and _NAME_RE.fullmatch(name) is not None:
            values[name] = resolved.value
        else:
            omit.append(name)
    log.debug(
        "project-env: project_id=%s user_id=%s resolved=%d omitted=%d omitted_names=%s",
        project_id, user_id, len(values), len(omit), ",".join(omit) or "-",
    )
    return SlotEnv(values=values, omit=omit)


def resolve_rows(
    session: Session, project_id: int, viewer_id: Optional[UUID]
) -> dict[UUID, RowStatus]:
    """How EVERY row of `project_id`'s catalogue resolves, keyed by row id.

    Every row, not the winners, each resolved under its own identity (D7) — so another user's row
    reports what their slot would get. `viewer_id` decides `winning` and nothing else."""
    rows = _rows(session, project_id)
    resolver = _Resolver(session, project_id)
    resolver.prime(rows)
    winners = _winners(rows, viewer_id)
    out: dict[UUID, RowStatus] = {}
    for row in rows:
        resolved = resolver.resolve(row)
        out[row.id] = RowStatus(
            status=resolved.status,
            detail=resolved.detail,
            # IDENTITY, not equality on `user_id`: `_winners` returns rows from this very list, and two
            # rows of one project can share a name across the two tiers.
            winning=winners.get(row.name) is row,
        )
    return out


def _rows(session: Session, project_id: int) -> list[ProjectEnvVar]:
    """One project's whole catalogue, both tiers, in a stable order.

    One query for both tiers (`ix_project_env_vars_project_id`), under `no_autoflush` — this is the first
    query of every resolution."""
    with session.no_autoflush:
        return list(
            session.scalars(
                select(ProjectEnvVar)
                .where(ProjectEnvVar.project_id == project_id)
                .order_by(ProjectEnvVar.name, ProjectEnvVar.id)
            )
        )


def _winners(rows: list[ProjectEnvVar], user_id: Optional[UUID]) -> dict[str, ProjectEnvVar]:
    """R4, the one place it is spelled: per name, `user_id`'s own override, else the project default.

    Another user's override is not a candidate at all. The two partial unique indexes make this
    deterministic."""
    winners: dict[str, ProjectEnvVar] = {}
    for row in rows:
        if row.user_id is not None and row.user_id != user_id:
            continue
        current = winners.get(row.name)
        if current is None or (current.user_id is None and row.user_id is not None):
            winners[row.name] = row
    return winners


class _Resolver:
    """One resolution pass over one project's catalogue, memoizing what it costs a query to learn.

    Memos are per pass so a revoked binding closes on the next resolution. `prime` groups the catalogue by
    resolving identity so the lease path pays one query per identity, not per credential."""

    def __init__(self, session: Session, project_id: int) -> None:
        self._session = session
        # The project a credential must be usable on (`authz.task_roster_select`).
        self._project_id = project_id
        self._principals: dict[Optional[UUID], Optional[Principal]] = {}
        self._credentials: dict[tuple[Optional[UUID], UUID], Optional[Credential]] = {}

    def prime(self, rows: Iterable[ProjectEnvVar]) -> None:
        """Load every credential these rows could reach, one query per distinct resolving identity.

        A pure optimization: it runs outside `resolve`'s guard, so the `except` keeps one unopenable
        credential (`Credential.config` raises `ValueError`) from failing the whole resolution. The cache
        stays unpopulated and `_reachable` retries per credential inside `resolve`. DB errors propagate."""
        wanted: dict[Optional[UUID], set[UUID]] = {}
        for row in rows:
            if row.source != PROJECT_ENV_VAR_SOURCE_CREDENTIAL or row.credential_id is None:
                continue
            wanted.setdefault(_identity_of(row), set()).add(row.credential_id)
        for identity, credential_ids in wanted.items():
            try:
                self._load(identity, credential_ids)
            except (SQLAlchemyError, TenancyScopeConflict):
                raise
            except Exception as exc:  # noqa: BLE001 — a batch must never decide the outcome.
                log.error(
                    "project-env: priming %d credential(s) failed (%s); falling back to per-row",
                    len(credential_ids), type(exc).__name__,
                )

    def resolve(self, row: ProjectEnvVar) -> _Resolved:
        """One row's outcome. Never raises over a bad row (see the guard at the bottom)."""
        try:
            if row.source != PROJECT_ENV_VAR_SOURCE_CREDENTIAL:
                # A literal needs no identity or query; `_usable` still applies since an unflushed
                # object has met no CHECK.
                if not _usable(row.literal):
                    return _Resolved(
                        REASON_FIELD_ABSENT, "this variable has no usable literal value"
                    )
                return _Resolved(STATUS_OK, None, str(row.literal))

            if row.credential_id is None:
                # The tombstone; `credential_field` survives so the operator can act on it.
                return _Resolved(
                    REASON_CREDENTIAL_MISSING,
                    "the credential this variable referenced no longer exists",
                )

            credential = self._reachable(_identity_of(row), row.credential_id)
            if credential is None:
                # Includes the system actor, which can use no credential.
                return _Resolved(
                    REASON_NOT_GRANTED,
                    "the identity this variable resolves under cannot use its credential in this project",
                )

            config: Mapping[str, Any] = credential.config or {}
            field = row.credential_field
            value = config.get(field) if field is not None else None
            if isinstance(value, SealedSecret) and is_nodevault_key_id(value.key_id):
                # Never opened here: the control plane holds no NodeVault key (D1). Read off `k` alone.
                return _Resolved(REASON_NODEVAULT, "NodeVault credential — not available to Jaah")
            if isinstance(value, SealedSecret):
                # A secret field is opened here, where it becomes the variable's value (the one seam,
                # `credential_crypto.open_secret`). One that cannot be opened is `field_absent` to an
                # operator: the row holds nothing usable, and re-entering it is the remedy.
                try:
                    value = open_secret(credential, field)
                except ValueError:
                    return _Resolved(
                        REASON_FIELD_ABSENT,
                        f"the referenced credential's secret field {field!r} could not be opened; "
                        "re-enter it",
                    )
            if not _usable(value):
                # Absent, null, `""` or non-string — one state to an operator.
                return _Resolved(
                    REASON_FIELD_ABSENT,
                    f"the referenced credential has no usable value for field {field!r}",
                )

            # Only the secret classification is logged, never the value (R10); `secret_keys` fails
            # closed. Level-guarded because the argument is evaluated eagerly inside the total guard.
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "project-env: %s resolved from a credential field (field=%r secret=%s)",
                    row.name, field, field in secret_keys(credential.type, config),
                )
            return _Resolved(STATUS_OK, None, str(value))
        except (SQLAlchemyError, TenancyScopeConflict):
            # Not a bad row: swallowing would fabricate `not_granted` for every remaining row on a
            # rollback-required session, and `SQLAlchemyError.__str__` leaks parameters.
            raise
        except Exception as exc:  # noqa: BLE001 — total over everything that is NOT the database.
            # Fail closed without a traceback: `not_granted` withholds. Logged by class only.
            log.error(
                "project-env: resolving %r failed (%s); withholding the variable",
                row.name, type(exc).__name__,
            )
            return _Resolved(
                REASON_NOT_GRANTED,
                "this variable could not be resolved and was withheld",
            )

    def _principal(self, identity: Optional[UUID]) -> Optional[Principal]:
        """The `Principal` whose bindings `identity` resolves under, or None when there is no such user.

        None means deny: `authz` reads a None principal as infrastructure (`true()`), so the caller
        branches instead. Memoized with `not in` so negative answers are cached too."""
        if identity is None:
            return None
        if identity not in self._principals:
            with self._session.no_autoflush:
                user = self._session.get(AdminUser, identity)
                self._principals[identity] = (
                    principal_for_user(self._session, user) if user is not None else None
                )
        return self._principals[identity]

    def _reachable(self, identity: Optional[UUID], credential_id: UUID) -> Optional[Credential]:
        """The credential `identity` may reach, or None. Cache read after `prime`, else a one-row load."""
        key = (identity, credential_id)
        if key not in self._credentials:
            self._load(identity, {credential_id})
        return self._credentials[key]

    def _load(self, identity: Optional[UUID], credential_ids: set[UUID]) -> None:
        """Resolve credential ids under one identity in one query, memoizing each (misses included).

        The rule lives in `authz.task_roster_select` (on this catalogue's project), never here. `no_autoflush`: this
        reads on the caller's session."""
        principal = self._principal(identity)
        found: dict[UUID, Credential] = {}
        if principal is not None:
            with self._session.no_autoflush:
                found = {
                    credential.id: credential
                    for credential in self._session.scalars(
                        task_roster_select(principal, self._project_id).where(
                            Credential.id.in_(credential_ids)
                        )
                    )
                }
        for credential_id in credential_ids:
            self._credentials[(identity, credential_id)] = found.get(credential_id)


def _identity_of(row: ProjectEnvVar) -> Optional[UUID]:
    """D7: an override resolves under its user, a project default under its last updater
    (`app/models/projects.py::ProjectEnvVar`). One function so `prime` and `resolve` read the same column."""
    return row.user_id if row.user_id is not None else row.updated_by_id
