"""Single-object access: `can_access`, `require_object_access`, and create-time ownership."""
from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from app.models.credentials import Account, Credential
from app.models.identity import AdminUser
from app.models.tasks import Task
from app.task_ancestry import may_stamp_starter
from app.tenancy.ids import is_tenant_org
from app.authz.catalog import ACTIONS, AUTHOR_DRAFT_KEY, permission_key
from app.authz.declarations import Declaration
from app.authz.principal import Principal, creates_org_rows, principal_for_user
from app.authz.visibility import (
    OBJECT_SPEC_BY_NAME,
    ObjectAccess,
    ObjectSpec,
    TASK_WRITE_SCOPE_ARM_KEYS,
    _EXPECTED_TASK_WRITE_ARM_ACTIONS,
    has_project_key,
    _entity_and_clause,
    authored_draft_task_clause,
    has_scope_key,
)


def named_user_reaches_project(
    session: Session, user: AdminUser, project_id: int, *, actor: AdminUser
) -> bool:
    """Whether naming `user` on `project_id` (a project role, a task assignee) is admissible for this
    `actor`: the target already reaches the project, judged with the target's superadmin flag dropped when a
    tenant actor names somebody other than themself.

    The one home of the naming probe, shared by `routes/projects.py::_validated_project_role` and
    `routes/tasks/create.py::_validated_assignee` so both accept exactly what `list_project_members` offers. The
    flag-drop stops either door being an `is_superadmin` oracle for a tenant; naming yourself keeps it; the
    actor's plane decides. A predicate, not a refusal: each door owes its own 422 sentence."""
    probe = principal_for_user(session, user)
    if is_tenant_org(actor.org_id) and user.id != actor.id:
        probe = replace(probe, is_superadmin=False)
    return can_access(session, probe, "project", project_id, mode=ObjectAccess.READ)


#: What Start's own route guard asks of the presser (`start_task`'s `@feature`), so of a stand-in too.
START_KEY = permission_key("tasks", "edit")


def named_user_may_start(
    session: Session, user: AdminUser, project_id: int, org_id: UUID | None, *, actor: AdminUser
) -> bool:
    """Whether an acting operator may name `user` as the starter of a task in `project_id` (`push_as_id`).

    The one home of the stand-in probe, shared by `routes/tasks/dispatch.py::_push_identity_or_422` (the refusal)
    and `routes/projects.py::list_project_members(may_start=True)` (the Start dialog's list), so the dialog offers
    only whom Start accepts: a live, non-system seat of `org_id` (`may_stamp_starter`, the git-token route's
    bound) who reaches the project and could have pressed Start there (`tasks:edit`)."""
    return (
        not user.is_system
        and user.disabled_at is None
        and may_stamp_starter(user, org_id)
        and named_user_reaches_project(session, user, project_id, actor=actor)
        and has_project_key(principal_for_user(session, user), START_KEY, project_id)
    )


def key_scope_satisfied(
    session: Session,
    principal: Principal | None,
    spec: ObjectSpec,
    object_id: int | UUID,
    *,
    key: str,
    mode: ObjectAccess,
) -> bool:
    """Is the key half of one row's decision satisfied? `True` for a type this mode does
    not key-scope.

    `key` must be held on the scope the row lives in (`key_scope_column`/`key_scope_type`) — org-wide, or
    bound there with inheritance applied. Both decision homes ask it: `authz_guard` and
    `slack_inbound._may_call`. Not folded into `can_access`, whose callers authorize other things than the
    route's key. A missing row is denied, indistinguishably from the clause probe (no existence oracle)."""
    if not spec.key_scoped_in(mode):
        return True
    if principal is None or (principal.has_org(key) and not principal.system_project_ids):
        # Allow-all or org-wide, short-circuited before the query — unless a system project is hidden from
        # this principal, which an org-wide key never reaches (`Principal.system_project_ids`).
        return True
    scope_id = session.scalar(select(spec.key_scope_column).where(spec.entity.id == object_id))
    return has_scope_key(principal, spec.key_scope_type, key, scope_id)


def can_access(
    session: Session,
    principal: Principal | None,
    object_type: str,
    object_id: int | UUID,
    *,
    mode: ObjectAccess,
) -> bool:
    """Shape 2 — the single-object probe. The entity goes in the enclosing `select_from`, so the clause
    correlates (never a cross join), and the clause's row
    gates (`hidden`, `unsaved`) cannot be forgotten.

    `object_id` must already be in `ObjectSpec.id_type`'s domain; a mismatch raises (500, not 404). `mode`
    is keyword-only with no default: WRITE iff the probe authorizes a mutation. The object question only:
    callers conjoin `key_scope_satisfied` where a key is exercised on the row (the guard does). Exception:
    the task clauses carry their key inside (see `key_scope_satisfied`); no other clause should."""
    entity, clause = _entity_and_clause(object_type, mode)
    return (
        session.scalar(
            select(literal(1))
            .select_from(entity)
            .where(entity.id == object_id)
            .where(clause(principal))
        )
        is not None
    )


def authored_draft(session: Session, principal: Principal | None, task_id: int) -> bool:
    """Is `task_id` a still-`draft` task `principal` authored? `can_access`'s shape, for
    `authored_draft_task_clause`.

    Public so `_enforce`'s fallback and `routes/tasks/`'s `authored_draft` projection (which offers the
    controls) agree; otherwise the button and the door disagree."""
    return (
        session.scalar(
            select(literal(1))
            .select_from(Task)
            .where(Task.id == task_id)
            .where(authored_draft_task_clause(principal))
        )
        is not None
    )


def author_draft_admits(
    session: Session,
    principal: Principal | None,
    spec: "ObjectSpec",
    task_id: int,
    *,
    mode: "ObjectAccess",
) -> bool:
    """The author-draft admission on one row: `task_id` is a still-`draft` top-level task
    `principal` authored (`authored_draft`) and `AUTHOR_DRAFT_KEY` is held on its project by the subject
    that granted it (`key_scope_satisfied`).

    Tasks are not grantable, so a `tasks:create`-only author is refused by `can_access(mode=WRITE)`; this
    is what the guard and `routes/tasks/dependencies.py::add_task_dependency` consult instead. The clause itself gains
    no author arm. No `AUTHOR_DRAFT_KEY` → refused before any query; `None`/superadmin are allow-all."""
    if principal is not None and not principal.has(AUTHOR_DRAFT_KEY):
        return False
    return key_scope_satisfied(
        session, principal, spec, task_id, key=AUTHOR_DRAFT_KEY, mode=mode
    ) and authored_draft(session, principal, task_id)


def readable_write_denial(object_type: str, *, key: str | None = None) -> str:
    """The one sentence a caller reads when refused a write on a row they can already see — raised by
    both `require_object_access` and `_enforce_object_params`.

    With `key`, the row is reachable but the key is not granted on its scope; without it, no grant reaches
    the row for writing. "Ask for access", since for a task the remedy is a key on its project."""
    if key is None:
        return f"You can see this {object_type} but not change it — ask for access to it."
    return f"You can see this {object_type} but not change it — your {key} was not granted on it."


def _refused_write_is_readable(
    session: Session,
    principal: Principal | None,
    object_type: str,
    object_id: int | UUID,
    mode: ObjectAccess,
    *,
    read_key: str | None = None,
) -> bool:
    """Was a refusal in `mode` the readable exception — a write on a row the caller may read?

    A 404 there would lie about a row the caller's own GET returns (same fix as
    `routes/credentials/guards.py::_scoped_row_or_404`). No oracle: the read probe runs only after a write refusal.
    "May read" is the whole read door: for types key-scoped on read (`project`, `feature`) it also asks
    `key_scope_satisfied` under `read_key`, and answers False when no read key is known."""
    if mode is not ObjectAccess.WRITE:
        return False
    if not can_access(session, principal, object_type, object_id, mode=ObjectAccess.READ):
        return False
    spec = OBJECT_SPEC_BY_NAME.get(object_type)
    if spec is not None and spec.key_scoped_in(ObjectAccess.READ):
        return read_key is not None and key_scope_satisfied(
            session, principal, spec, object_id, key=read_key, mode=ObjectAccess.READ
        )
    return True


def require_object_access(
    session: Session,
    principal: Principal | None,
    object_type: str,
    object_id: int | UUID,
    *,
    mode: ObjectAccess,
) -> None:
    """`can_access` or 404 — or 403 for a write on a row the caller can read. `mode` is keyword-only
    with no default.

    404, not 403: a 403 on an invisible object confirms it exists. Feature denial is 403, object denial
    404. The readable exception (`_refused_write_is_readable`) answers 403 with `readable_write_denial`, as
    `_enforce_object_params` does; no `read_key` is passed here, so read-key-scoped types keep the flat 404.
    The handler-side helper: it always raises `HTTPException`; the guard probes `can_access` itself."""
    if not can_access(session, principal, object_type, object_id, mode=mode):
        if _refused_write_is_readable(session, principal, object_type, object_id, mode):
            raise HTTPException(status_code=403, detail=readable_write_denial(object_type))
        raise HTTPException(status_code=404, detail="not found")


#: The two answers of a create door's `ownership` field (G2/G3): the caller's own row, or the org's.
OWNERSHIP_PERSONAL = "personal"
OWNERSHIP_ORG = "org"
OWNERSHIP_CHOICES = (OWNERSHIP_PERSONAL, OWNERSHIP_ORG)
#: A credential's third answer (0325): an org row the control plane acts with (`Credential.system`).
#: Credentials only — an account has no system kind.
OWNERSHIP_SYSTEM = "system"
CREDENTIAL_OWNERSHIP_CHOICES = (OWNERSHIP_PERSONAL, OWNERSHIP_ORG, OWNERSHIP_SYSTEM)


def validated_ownership(
    value: Any, *, may_create_org: bool, choices: tuple[str, ...] = OWNERSHIP_CHOICES
) -> str | None:
    """A create door's `ownership` field: `None` when absent (the caller's default), else one of
    `choices` (`CREDENTIAL_OWNERSHIP_CHOICES` for a credential). A 422 names the field, never the value; a
    caller who may not create an org row asking for one (or a system row, which is an org row) is a 403
    rather than a silent personal row — they would believe the org shares it."""
    if value is None:
        return None
    if value not in choices:
        raise HTTPException(
            status_code=422, detail=f"ownership must be one of {list(choices)}"
        )
    if value in (OWNERSHIP_ORG, OWNERSHIP_SYSTEM) and not may_create_org:
        raise HTTPException(
            status_code=403,
            detail=(
                "only an Org Owner or Admin creates an org row — yours is personal: omit ownership "
                "or send 'personal'"
            ),
        )
    return str(value)


def validated_owner_user(
    session: Session,
    principal: Principal | None,
    owner_user_id: UUID | None,
    *,
    ownership: str,
    org_id: UUID | None,
) -> UUID | None:
    """A credential create's `owner_user_id` (D6): the org member a PERSONAL row is made for, or `None`.

    Only with `ownership == "personal"` (else 422). Naming yourself is always allowed; naming anyone else
    needs `Principal.is_org_admin` — a tenant's Org Owner/Admin, or an operator (acting as the tenant
    included) — else 403, asked before the lookup so a refused caller learns nothing about who exists. The
    named user must be an enabled, non-system member of `org_id` — the caller's org, the acting seat's for an operator acting as a
    tenant — else 422, one answer for unknown and foreign alike."""
    if owner_user_id is None:
        return None
    if ownership != OWNERSHIP_PERSONAL:
        raise HTTPException(status_code=422, detail="owner_user_id needs ownership 'personal'")
    if principal is not None and owner_user_id == principal.user_id:
        return owner_user_id
    # `is_org_admin`, not `creates_org_rows`: an org role on the operator plane administers nobody.
    if principal is not None and not principal.is_org_admin:
        raise HTTPException(
            status_code=403,
            detail="only an Org Owner or Admin creates a credential for someone else — omit owner_user_id",
        )
    # `admin_users` is never seam-narrowed (`seam.ORG_FILTER_EXEMPT_CLASSES`): the org test is this one.
    owner = session.scalar(select(AdminUser).where(AdminUser.id == owner_user_id))
    if (
        owner is None or owner.is_system or owner.disabled_at is not None
        or org_id is None or owner.org_id != org_id
    ):
        raise HTTPException(status_code=422, detail="owner_user_id must be a member of this org")
    return owner_user_id


def stamp_owner_on_create(
    principal: Principal | None,
    obj: Credential | Account,
    ownership: str | None = None,
    *,
    owner_user_id: UUID | None = None,
) -> None:
    """Decide whether a credential or account `principal` just created is PERSONAL or the ORG's (G3/E2).

    A caller with no org role (a project member bringing their own) gets `owner_id = caller` — personal,
    usable by them alone. An Org Owner/Admin or an operator leaves `owner_id` NULL — an org row, which
    every Org Owner/Admin reaches. The agent principal (`None`) owns nothing. Call before the flush, in the
    caller's transaction; an owner a caller already set is never overwritten.

    `ownership` is the caller's explicit choice, already through `validated_ownership`: `personal` makes
    the row the caller's whoever they are — or `owner_user_id`'s, already through `validated_owner_user`; `system` makes a credential an unowned SYSTEM row
    (`Credential.system`); `org` or `None` takes the default above.

    Replaces the creator grant: there is no grant to write any more, only the ownership column the reads
    consult (`owned_credential_clause`, `visible_account_clause`, `usable_credential_clause`)."""
    if not isinstance(obj, (Credential, Account)):
        raise ValueError(
            f"cannot stamp an owner on {type(obj).__name__}; only a Credential or an Account is personal"
        )
    if obj.owner_id is not None:
        return
    if owner_user_id is not None:
        obj.owner_id = owner_user_id
        return
    if ownership == OWNERSHIP_SYSTEM:
        if not isinstance(obj, Credential) or not creates_org_rows(principal):
            raise ValueError("only an Org Owner/Admin or an operator creates a system credential")
        obj.system = True
        return
    if principal is None:
        return
    if ownership != OWNERSHIP_PERSONAL and creates_org_rows(principal):
        return
    obj.owner_id = principal.user_id


# Which task clause each action means, derived from the declaration's verb. `view` -> READ holds for
# routes reaching this map; `routes/console.py`'s credential-minting `…/direct` routes are
# `@feature("worktrees", "view")` without `object_type` — adding `object_params` there would bind the
# permissive clause, so re-declare the verb instead.
_ACTION_OBJECT_ACCESS = {
    "view": ObjectAccess.READ,
    "create": ObjectAccess.WRITE,
    "edit": ObjectAccess.WRITE,
    "delete": ObjectAccess.WRITE,
}

# Every action classified, checked at import (a fifth would silently default to WRITE).
if set(_ACTION_OBJECT_ACCESS) != set(ACTIONS):
    raise RuntimeError(
        "_ACTION_OBJECT_ACCESS and ACTIONS have drifted: "
        f"{sorted(set(ACTIONS) ^ set(_ACTION_OBJECT_ACCESS))}"
    )

# And `TASK_WRITE_SCOPE_ARM_KEYS` must mirror the WRITE actions minus `create` (which names no
# existing row). Asserted, not derived, so a new write action forces a decision instead of silently
# reaching the console keyboard.
_TASK_WRITE_ACTIONS = {a for a, m in _ACTION_OBJECT_ACCESS.items() if m is ObjectAccess.WRITE} - {"create"}
if _TASK_WRITE_ACTIONS != _EXPECTED_TASK_WRITE_ARM_ACTIONS:
    raise RuntimeError(
        "the task-WRITE actions changed but _EXPECTED_TASK_WRITE_ARM_ACTIONS did not: a write action "
        "this arm does not admit is a declaring route whose key `editable_task_clause` silently refuses"
    )
# And the constant itself is compared, not just a literal, so it cannot be edited down unnoticed.
if {permission_key("tasks", a) for a in _EXPECTED_TASK_WRITE_ARM_ACTIONS} != set(TASK_WRITE_SCOPE_ARM_KEYS):
    raise RuntimeError(
        "TASK_WRITE_SCOPE_ARM_KEYS does not match the task-WRITE actions it claims to mirror: "
        f"{sorted(TASK_WRITE_SCOPE_ARM_KEYS)} vs {sorted(_EXPECTED_TASK_WRITE_ARM_ACTIONS)} — "
        "`editable_task_clause`'s scope arm would refuse a key a declaring route requires"
    )


def object_access_for(declaration: Declaration) -> ObjectAccess:
    """Which object clause a declaration's action means — the one mapping, read by the guard and by
    in-process callers applying the same declaration (`slack_inbound._may_answer`). An unclassified action
    falls to WRITE, the narrower one."""
    return _ACTION_OBJECT_ACCESS.get(declaration.action or "", ObjectAccess.WRITE)
