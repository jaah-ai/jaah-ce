"""Credential bulk actions — delete, move to folder, change org — over a set of up to `BULK_CAP` rows.

The set is either `ids` the caller picked or `match`, the roster's own filters (`roster.RosterFilterSet`), resolved
through `roster.roster_where` under the caller's roster scope: a bulk action never reaches a row the roster would not
list. `expect_count` is what the caller saw; any other count refuses (`set_changed`), so nothing the caller did not
review is touched.

ALL OR NOTHING. The set is locked (`FOR UPDATE`), every refusal is asked of the whole set in a fixed number of
queries, then the write runs through the single-row routes' own set functions (`writes.delete_credentials`,
`writes.rename_credentials`), so each row writes the audit event its single-row action writes. A refusal is a
`detail` object: `reason`, `blocked` (how many rows block), `message` (the sentence the console shows).
"""
from __future__ import annotations

from typing import Any, NoReturn, Optional, Sequence
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app import credential_consumers, edition_features
from app.actors import stamp_actor
from app.auth import require_operator, require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature, superadmin_only
from app.authz.principal import Principal, current_principal
from app.authz.visibility import credential_write_clause
from app.db import get_session
from app.entity_shape import StrictModel
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.routes.credentials.credential_wire import (
    DuplicateName,
    _NAME_MAX_LEN,
    _audit,
    _refuse_duplicates_in_home,
    _scrubbed_write,
    _validated_name,
)
from app.routes.credentials.guards import _DELETE_KEY, _EDIT_KEY
from app.routes.credentials.roster import RosterFilterSet, _uuid_array, roster_scope, roster_where
from app.routes.credentials.writes import _PATH_SEPARATOR, _name_segments, delete_credentials, rename_credentials
from app.tenancy import seam

router = APIRouter(tags=["credentials"])

#: The most rows one action touches. Patched by tests.
BULK_CAP = 10_000

SET_CHANGED = "The matching set changed — review and retry"


class BulkIn(StrictModel):
    """Exactly one of `ids` (rows the caller picked) or `match` (every row the roster lists under those filters).
    `expect_count` is the count the caller confirmed. None of these is a secret."""

    ids: Optional[list[UUID]] = None
    match: Optional[RosterFilterSet] = None
    expect_count: int


class BulkMoveIn(BulkIn):
    """`to`: the folder every row lands in, keeping its own leaf name; empty or absent is the top level."""

    to: Optional[str] = None


class BulkChangeOrgIn(BulkIn):
    org_id: UUID


class BulkOut(BaseModel):
    count: int


def _refuse(status: int, reason: str, blocked: int, message: str, **facts: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"reason": reason, "blocked": blocked, "message": message, **facts})


def _plural(n: int) -> str:
    return f"{n:,} credential{'' if n == 1 else 's'}"


def _names(rows: Sequence[Any], limit: int = 3) -> str:
    """The first few rows' names, for a refusal that names what blocks."""
    names = ", ".join(row.name for row in rows[:limit])
    return names if len(rows) <= limit else f"{names} and {len(rows) - limit:,} more"


def match_statement(where: Sequence[Any]) -> Select:
    """A `match`'s ids: at most `BULK_CAP + 1` (one more says "over the cap"), walking the roster's newest-first
    index like its capped count. The roster EXPLAIN oracle (in git history) ran it as the `bulk` case."""
    return (
        select(Credential.id).where(*where)
        .order_by(Credential.created_at.desc(), Credential.id.desc()).limit(BULK_CAP + 1)
    )


def _locked_set(
    session: Session, principal: Principal | None, body: BulkIn, key: Optional[str] = None, verb: str = "",
) -> list[Credential]:
    """The rows `body` names, locked in id order (two bulk writers never deadlock), or the refusal: not exactly one
    of `ids`/`match` (422), over `BULK_CAP` (422 `BULK_CAP`), not `expect_count` rows (409 `set_changed`), or — with
    `key` — a row the caller's `key` scope does not reach (403 `forbidden`, `_refuse_unpermitted`). That last is asked
    BEFORE the lock, so a caller who may only read a set never holds its rows."""
    if (body.ids is None) == (body.match is None):
        raise HTTPException(status_code=422, detail="send exactly one of ids or match")
    if body.match is not None:
        where = roster_where(session, principal, body.match.to_filters())
        ids = list(session.scalars(match_statement(where)))
    else:
        ids = list(dict.fromkeys(body.ids or []))
        if len(ids) != len(body.ids or []):
            raise HTTPException(status_code=422, detail="ids must not repeat")
        where = [roster_scope(principal, mine=False)]
    if len(ids) > BULK_CAP:
        _refuse(422, "BULK_CAP", len(ids), f"At most {BULK_CAP:,} credentials per action. Narrow the filter.")
    if not ids and body.expect_count == 0:
        raise HTTPException(status_code=422, detail="no credentials selected")
    found = session.execute(
        select(Credential.id, Credential.name).where(*where, Credential.id == _uuid_array(ids))
    ).all() if ids else []
    if len(found) != body.expect_count or len(found) != len(ids):
        _refuse(409, "set_changed", abs(len(found) - body.expect_count), SET_CHANGED)
    if key is not None:
        _refuse_unpermitted(session, principal, found, key, verb)
    rows = list(session.scalars(
        select(Credential).where(*where, Credential.id == _uuid_array(ids)).order_by(Credential.id).with_for_update()
    ))
    if len(rows) != len(found):
        # Deleted or re-scoped between the read and the lock.
        _refuse(409, "set_changed", len(found) - len(rows), SET_CHANGED)
    return rows


def _refuse_unpermitted(session: Session, principal: Principal | None, rows: Sequence[Any], key: str, verb: str) -> None:
    """403 `forbidden` unless the caller's `key` scope (`credential_write_clause`, the single-row routes' own)
    reaches every one of `rows` (each with an `id` and `name`)."""
    permitted = set(session.scalars(
        select(Credential.id).where(Credential.id == _uuid_array([row.id for row in rows]),
                                    credential_write_clause(principal, key))
    ))
    blocked = [row for row in rows if row.id not in permitted]
    if blocked:
        _refuse(403, "forbidden", len(blocked), f"You can't {verb} {_plural(len(blocked))}: {_names(blocked)}.")


def _refuse_in_use(rows: Sequence[Credential], used_by: dict[UUID, list[str]]) -> NoReturn:
    """409 `consumer`: the rows `used_by` names, each with what uses it (`consumers` maps id → labels)."""
    used = [row for row in rows if row.id in used_by]
    named = "; ".join(f"{row.name} ({', '.join(used_by[row.id])})" for row in used[:3])
    more = f" and {len(used) - 3:,} more" if len(used) > 3 else ""
    _refuse(409, "consumer", len(used), f"{_plural(len(used))} in use — detach first: {named}{more}.",
            consumers={str(row.id): used_by[row.id] for row in used})


@router.post("/api/credentials/bulk/delete", response_model=BulkOut)
@feature("credentials", "delete", own_credential=True)
def bulk_delete_credentials(
    body: BulkIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> BulkOut:
    """Delete the set, as `DELETE /api/credentials/{id}` deletes one row: tombstoned, audited, and refused while a
    row backs Jaah-managed storage (`nodelinks.managed_credentials`). A row a consumer's RESTRICT key holds is a
    409 naming it (`credential_consumers.delete_blockers`), where the single-row route meets the database's refusal."""
    rows = _locked_set(session, principal, body, _DELETE_KEY, "delete")
    # The community edition has no Jaah-managed NodeLink storage.
    managed: set[UUID] = set()
    if edition_features.enabled("nodelink"):
        from app import nodelinks

        managed = nodelinks.managed_credentials(session, [row.id for row in rows])
    if managed:
        blocked = [row for row in rows if row.id in managed]
        _refuse(409, "managed", len(blocked),
                f"{_names(blocked)} backs Jaah-managed NodeLink storage. Replace it in Settings first.")
    blockers = credential_consumers.delete_blockers(session, [row.id for row in rows])
    if blockers:
        _refuse_in_use(rows, blockers)
    delete_credentials(session, user, rows)
    return BulkOut(count=len(rows))


@router.post("/api/credentials/bulk/move-folder", response_model=BulkOut)
@feature("credentials", "edit", own_credential=True)
def bulk_move_credentials(
    body: BulkMoveIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> BulkOut:
    """File every row of the set under the folder `to`, keeping its leaf name: `a/b/x` moved to `c` is `c/x`. A
    renamed name colliding in its home refuses the whole set (409 `name_collision`, naming it)."""
    if len(body.to or "") > _NAME_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"a folder must be at most {_NAME_MAX_LEN} characters")
    dest = _name_segments(body.to or "")
    rows = _locked_set(session, principal, body, _EDIT_KEY, "change")
    renamed = {
        row.id: _validated_name(_PATH_SEPARATOR.join([*dest, (_name_segments(row.name) or [row.name])[-1]]))
        for row in rows
    }
    try:
        rename_credentials(session, user, rows, renamed)
    except DuplicateName as exc:
        _refuse(409, "name_collision", 1, f"A credential named {exc.name} is already there.", name=exc.name)
    return BulkOut(count=len(rows))


@router.post("/api/credentials/bulk/change-org", response_model=BulkOut)
@superadmin_only(Tenancy.OPERATOR)
def bulk_change_credential_org(
    body: BulkChangeOrgIn,
    user: AdminUser = Depends(require_operator),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> BulkOut:
    """[op] Move the set into the org `org_id`. Operator plane only (not acting as a tenant). Refused (409, each
    naming its reason) for a row that is `system`, homed in a project, personal with an owner outside the target
    org, NodeVault (its envelopes are sealed under the source org's key), or pointed at by anything
    (`credential_consumers.references_of`, naming each). The KMS envelopes' AAD is id, field and destination, never
    the org, so a moved row's config still opens."""
    if principal is None or not principal.is_operator or seam.current_scope() is not seam.OPERATOR:
        _refuse(403, "forbidden", body.expect_count, "Only an operator can change a credential's org.")
    target = session.execute(select(Org.id, Org.name).where(Org.id == body.org_id)).first()
    if target is None:
        raise HTTPException(status_code=422, detail="org_id is not an org")
    rows = _locked_set(session, principal, body)
    for reason, blocked, message in (
        ("system", [row for row in rows if row.system], "system credentials can't change org"),
        ("nodevault", [row for row in rows if row.nodevault],
         "NodeVault credentials can't change org: their secrets open only on this org's nodes"),
        ("project_home", [row for row in rows if row.home_project_id is not None],
         "credentials belong to a project. Move them out of it first"),
    ):
        if blocked:
            _refuse(409, reason, len(blocked), f"{_plural(len(blocked))} — {message}: {_names(blocked)}.")
    owners = {row.owner_id for row in rows if row.owner_id is not None}
    members = set(session.scalars(
        select(AdminUser.id).where(AdminUser.id == _uuid_array(list(owners)), AdminUser.org_id == target.id)
    )) if owners else set()
    outsiders = [row for row in rows if row.owner_id is not None and row.owner_id not in members]
    if outsiders:
        _refuse(409, "non_member_owner", len(outsiders),
                f"{_plural(len(outsiders))} {'belongs' if len(outsiders) == 1 else 'belong'} to people outside {target.name}: "
                f"{_names(outsiders)}.")
    references = credential_consumers.references_of(session, rows)
    if references:
        _refuse_in_use(rows, references)
    try:
        _refuse_duplicates_in_home(session, [(row, row.name, None) for row in rows], org_id=target.id)
    except DuplicateName as exc:
        _refuse(409, "name_collision", 1, f"{target.name} already has a credential named {exc.name}.", name=exc.name)

    sources = dict(session.execute(
        select(Org.id, Org.name).where(Org.id == _uuid_array(list({row.org_id for row in rows if row.org_id})))
    ).tuples().all())
    with _scrubbed_write(session, "move"):
        for row in rows:
            source = row.org_id
            # Through the ORM, so the tenancy write seam sees the change (unbound: an explicit org is kept).
            row.org_id = target.id
            # The operator becomes its creator: the target org must not read a source-org member's name on it
            # (an org row has no owner; the audit row keeps the history).
            row.created_by_id = user.id
            stamp_actor(row, user)
            _audit(
                session, user, "credential_updated",
                f"Moved credential {row.name} from {sources.get(source, 'no org')} to {target.name}", row,
                extra={"from_org_id": source, "to_org_id": target.id},
            )
        session.commit()
    return BulkOut(count=len(rows))
