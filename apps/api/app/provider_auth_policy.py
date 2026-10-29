"""The provider grid's catalogue, verdict and refusal sentence — the doors' one API.

Resolution (override → plan → `(default)` → `SEED`, the operator plane included): `app/plan_terms.py`'s `providers`
term, read here through `plan_terms.providers_of` and nothing else. A cell is `(row, auth)`: a built-in provider value
(`agent_providers.PROVIDERS`) with one of its `auth_kinds`, `SDK_ROW` with `OWN`, or `REGISTRY_PREFIX` + an ACP-registry
id with `api_key`. A cell is ON only while stored; an existing account on an OFF cell is never edited or deleted, only
never leased (`routes/fleet.py::_account_candidates`'s `auth_pair_disallowed`).
"""
from __future__ import annotations

from typing import Any, Final, Iterable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import account_auth, acp_registry, agent_providers
from app.tenancy.ids import is_tenant_org

Cell = tuple[str, str]

#: The "SDK agent" row: manifest agents and catalog connectors, which lease no account (`OWN`).
SDK_ROW: Final = "sdk"
OWN: Final = "own"
#: A registry agent's row is `acp:<registry id>`, so it never collides with a built-in value (`pi` vs `pi-acp`).
REGISTRY_PREFIX: Final = "acp:"

#: The grid with nothing stored anywhere: Claude Code on an API key.
SEED: Final[frozenset[Cell]] = frozenset({(agent_providers.CLAUDE.value, account_auth.AUTH_KIND_API_KEY)})

_AUTH_LABELS: Final = {
    account_auth.AUTH_KIND_OAUTH: "OAuth", account_auth.AUTH_KIND_API_KEY: "API key", OWN: "own credentials",
}


def builtin_cells() -> tuple[Cell, ...]:
    """Every built-in cell this build can serve, in catalogue order."""
    return tuple((p.value, kind) for p in agent_providers.PROVIDERS for kind in p.auth_kinds)


supported_pairs = builtin_cells


def _known(cell: Cell) -> bool:
    """The decode rule: a built-in pair this build serves, `sdk`/`own`, or a well-formed `acp:<id>`/`api_key`. Shape
    only for a registry row: the registry is never fetched here, so neither the plan editor nor dispatch waits on it."""
    row, auth = cell
    if row == SDK_ROW:
        return auth == OWN
    if row.startswith(REGISTRY_PREFIX):
        return auth == account_auth.AUTH_KIND_API_KEY and acp_registry.is_registry_id(row[len(REGISTRY_PREFIX):])
    return cell in builtin_cells()


def decoded(raw: Any) -> frozenset[Cell]:
    """A stored list of `[row, auth]` cells; a malformed or no-longer-supported one is dropped (reads OFF)."""
    if not isinstance(raw, list):
        return frozenset()
    cells = ((item[0], item[1]) for item in raw
             if isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(v, str) for v in item))
    return frozenset(cell for cell in cells if _known(cell))


def validated(cells: Iterable[Cell]) -> frozenset[Cell]:
    """`cells`, or 422 naming the first malformed or unsupported one. Nothing is written on a 422."""
    chosen = frozenset((str(row), str(auth)) for row, auth in cells)
    for row, auth in sorted(chosen):
        if not _known((row, auth)):
            raise HTTPException(status_code=422, detail=f"{row_label(row)} does not take auth type '{auth}'.")
    return chosen


def _order(cell: Cell) -> tuple[int, int, str]:
    builtins = builtin_cells()
    if cell in builtins:
        return 0, builtins.index(cell), ""
    return (1, 0, "") if cell[0] == SDK_ROW else (2, 0, cell[0])


def encoded(cells: Iterable[Cell]) -> list[list[str]]:
    """The stored (and wire) form, in a stable order: built-ins (catalogue), `sdk`, then `acp:*` sorted."""
    return [[row, auth] for row, auth in sorted(set(cells), key=_order)]


def allowed(session: Session, org_id: Optional[UUID], *, memo: Optional[dict] = None) -> frozenset[Cell]:
    """The cells `org_id` may run (`plan_terms.providers_of`); `memo` (keyed by org) spares a per-account caller
    repeat reads."""
    from app import plan_terms

    return plan_terms.providers_of(session, org_id, memo=memo)


def is_allowed(session: Session, org_id: Optional[UUID], row: str, auth: str, *, memo: Optional[dict] = None) -> bool:
    return (row, auth) in allowed(session, org_id, memo=memo)


def provider_on_plan(session: Session, org_id: Optional[UUID], row: str, *, memo: Optional[dict] = None) -> bool:
    """Whether any cell of `row` is allowed: what a provider pin (a task, an automation, a project default, a console
    pick, a sign-in of an existing account) and the agent picker ask."""
    return any(cell_row == row for cell_row, _ in allowed(session, org_id, memo=memo))


def registry_row(registry_id: str) -> str:
    """The row of the ACP-registry agent `registry_id` (an installed registry agent's slug)."""
    return f"{REGISTRY_PREFIX}{registry_id}"


def agents_on(session: Session, org_id: Optional[UUID], *, memo: Optional[dict] = None) -> bool:
    """Whether any agent row is ON — `SDK_ROW` or any registry row: what opens the Agents pages."""
    return any(row == SDK_ROW or row.startswith(REGISTRY_PREFIX) for row, _ in allowed(session, org_id, memo=memo))


def registry_on(session: Session, org_id: Optional[UUID], *, memo: Optional[dict] = None) -> bool:
    """Whether any registry row is ON: what opens the registry browser."""
    return any(row.startswith(REGISTRY_PREFIX) for row, _ in allowed(session, org_id, memo=memo))


def row_label(row: str) -> str:
    """How people name a row: the provider's label, "SDK agent", or the registry id (a caller holding the registry
    entry's name passes it to `refusal` as `label`)."""
    if row == SDK_ROW:
        return "SDK agent"
    if row.startswith(REGISTRY_PREFIX):
        return row[len(REGISTRY_PREFIX):]
    record = agent_providers.by_value(row)
    return record.label if record is not None else row


def auth_label(auth: str) -> str:
    return _AUTH_LABELS.get(auth, auth)


def cell_label(cell: Cell) -> str:
    """One cell as the org's history records it: "<provider> · OAuth"."""
    return f"{row_label(cell[0])} · {auth_label(cell[1])}"


def refusal(
    session: Session, org_id: Optional[UUID], row: str, auth: Optional[str] = None, *, label: Optional[str] = None,
) -> str:
    """The sentence a door refuses an OFF cell (or, with no `auth`, a row with no cell ON) with: what, and the remedy
    — Billing for a tenant, the org's Providers override for an operator-plane org. `label` names the row in place of
    `row_label` (a registry agent's name)."""
    name = label or row_label(row)
    return off_sentence(org_id, name if auth is None else f"{name} with {auth_label(auth)}")


def off_sentence(org_id: Optional[UUID], what: str, *, plural: bool = False) -> str:
    """"<what> is not on your plan. Upgrade in Billing." for a tenant; the Providers override for the operator plane."""
    verb = "are" if plural else "is"
    if is_tenant_org(org_id):
        return f"{what} {verb} not on your plan. Upgrade in Billing."
    return f"{what} {verb} off in this organization's Providers override."


def require_allowed(
    session: Session, org_id: Optional[UUID], row: str, auth: str, *, label: Optional[str] = None,
) -> None:
    """422 `refusal` unless `org_id` may run `(row, auth)`: the account-create and agent-install doors."""
    if not is_allowed(session, org_id, row, auth):
        raise HTTPException(
            status_code=422, detail=f"{refusal(session, org_id, row, auth, label=label)} Nothing was created.",
        )
