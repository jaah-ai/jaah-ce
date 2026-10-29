"""The Audit page's Activity tab — one browsable, searchable, filterable view of `activity_events`.

`GET /api/audit`: one keyset page of the activity trail, newest first, with a count and the labels its
filter chips need. `GET /api/audit/suggest`: the search box's facet suggestions (users, orgs, records).
`GET /api/audit/events/{id}`: one row. All gated `audit:view` (the key's history: `app/authz/`'s
org-admin tier block). The single `/api/audit` prefix gives the capture fences one string to exclude
(`app/user_activity.py::READ_EXCLUDED_PREFIXES`).

Built for 10M rows: every arm of a page read is an index probe. Paging is a `(created_at, id)`
cursor, never an offset; `total` is exact when the first page holds everything, else the operator's is the
planner's estimate and a tenant's a count capped at `_COUNT_CAP` (`total_kind`); `q` is one expression (`app/models/audit.py::activity_search_text`) under a trigram index; `subject`
is a JSONB containment under a `jsonb_path_ops` index.

Flat by decision (D4) on the permission axis: a holder sees every row their org admits — everyone's
actions and payloads, for tasks they may not read: the `task:updated` `changes` map (before/after
`title`/`body`), `task_note:answered` Q&A, and `task_note:outcome` summaries and `no_pr_evidence`. It is a
NAMED follow-up requiring an
operator call on D4's scope. The tenancy seam narrows by org on every select; the `org_id` filter only narrows
within what the seam admits, never in its place. The one statement the seam cannot see, the EXPLAIN behind
the estimate, is the operator's alone: the planner reads table-wide statistics, so a tenant's estimate would
leak other orgs' values through `total` whatever predicate it carried (`_estimated_rows`).
What a row points at keeps its own rules: `subject.label` via each type's scoped select
(`_SUBJECT_MODELS`), `data.account_slug` via the account's own access control (`audit_visibility.visible_payload`).
Org names (each row's `org`, the org facet) are the operator's alone.

Two row populations, both served: captured request rows (actor, path, method, status, handler `kind`)
and machine rows (`task:%`/`task_note:%`, NULL user/status, empty transition summary — hence the
server-derived `label` and `subject`). One task mutation can produce both (D17).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from itertools import chain, zip_longest
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, lateral, not_, or_, select, true, tuple_, values
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import ClauseElement, Executable, column

from app.identities import label_expr
from app.activity_kinds import SUBJECT_PARAMS, nodevault_label
from app.auth import require_user
from app.authz.catalog import PROJECTS_VIEW_KEY, permission_key
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import (
    granted_account_slugs,
    readable_account_select,
    scoped_credential_select,
    scoped_node_select,
    scoped_project_select,
    scoped_task_select,
)
from app import edition_features, keyset, login_lockout, plan_terms, request_caller
from app.db import get_session
from app.models.access import Group
from app.models.audit import (
    SIGN_IN_REFUSAL_KIND,
    ActivityEvent,
    activity_search_text,
    refusal_identifier_text,
    refusal_kind_clause,
)
from app.models.automations import Cron
from app.models.credentials import Account, Credential
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import Project
from app.models.task_extras import Tag
from app.models.tasks import Task
from app.audit_visibility import row_visible, tenant_worded, visible_payload
from app.routes.tasks.activity import _transition_label
from app.tenancy import seam
from app.tenancy.ids import UNATTRIBUTED_SIGN_IN_ORG_ID, is_tenant_org
from app.entity_shape import UuidRefOut, from_row

log = logging.getLogger("app.routes.audit")

router = APIRouter(prefix="/api/audit", tags=["audit"])

#: `user_id=system` on the wire means `user_id IS NULL` (the machine rows): a value in the user facet
#: (D24), never a valid uuid. Less a refused sign-in that names its attempt (`SIGN_IN_REFUSAL_KIND`): actorless,
#: but the Activity tab reads it as that address, so "System" would list a person. A known user's own
#: chip reaches the refusals naming their email instead; an unknown address is reached by search, never offered as
#: a chip — it is whatever a stranger typed.
SYSTEM_ACTOR = "system"
SYSTEM_ACTOR_LABEL = "System"

#: The sign-in security records (`app/login_funnel.py`): actorless, filed under the refused user's org, or
#: `UNATTRIBUTED_SIGN_IN_ORG_ID` when the attempt resolved no user — which the operator reads as unattributed, not
#: as the operator org.
_SIGN_IN_SECURITY_KINDS = (SIGN_IN_REFUSAL_KIND, "login_locked")

#: The key `GET /api/tags/groups` carries (`routes/tags.py`) — `tasks:view`, not `tags:view`: the
#: catalogue read stayed on `tasks` when authoring moved to `tags:*`. Mirror that route, not the noun.
TASKS_VIEW_KEY = permission_key("tasks", "view")

_ACTION_TYPES = ("all", "read", "write")
_Q_MAX_LEN = 200
#: Activity's `q` floor: `pg_trgm` indexes three-character grams, so shorter text cannot use the trigram index
#: and a rare match walks the whole trail.
_Q_MIN_LEN = 3
_PER_MAX = 200
#: Per list in `GET /api/audit/suggest`.
_SUGGEST_MAX = 6
#: Values per repeatable facet (`user_id`, `org_id`): each is a bind, and the chips a person builds are few.
_FACET_MAX = 50
#: Where a tenant's count stops: an exact count past it is a walk of the org's trail per page.
_COUNT_CAP = 10_000
#: A tenant's capped count past this gives up and reports a floor: the trigram index spans every org, so a
#: common `q` can make the 10,001-row count walk other orgs' matches first.
_COUNT_TIMEOUT = "2s"


# ── The subject join ──────────────────────────────────────────────────────────────────────────────
#
# A row names its subject as `data.subject_ref = {"type", "id"}` (capture middleware), the flat
# `{"entity": "task", "id"}` (machine rows), or a `{"<noun>_id": …}` key (`record_write` sites). The
# label says what happened; `subject` says to which.

#: type → (id column, label column, scoped SELECT). A label is a read of the object, so it goes through
#: that type's own access clause (D4 does not widen object grants); unresolvable → `None`. `user`,
#: `group`, `cron` are org-admin-only like their writes (the roster reads in `routes/users.py` /
#: `routes/groups.py` are broader — a named follow-up); `tag` needs `tasks:view`. Key set pinned to `SUBJECT_PARAMS` by
#: `test_audit_routes.py`.
_SUBJECT_MODELS: dict[str, tuple[Any, Any, Any]] = {
    "task": (Task.id, Task.title, scoped_task_select),
    "project": (
        Project.id, Project.name,
        lambda principal: scoped_project_select(principal, key=PROJECTS_VIEW_KEY),
    ),
    "credential": (Credential.id, Credential.name, scoped_credential_select),
    "node": (Node.id, Node.name, scoped_node_select),
    # `readable_account_select`, the one home for what an account read means.
    "account": (Account.id, Account.name, readable_account_select),
    # The email: what operators recognise and what `record_write` stores in `actor_label`.
    "user": (AdminUser.id, AdminUser.email, lambda principal: _org_admin_only(principal, AdminUser)),
    "group": (Group.id, Group.name, lambda principal: _org_admin_only(principal, Group)),
    "cron": (Cron.id, Cron.name, lambda principal: _org_admin_only(principal, Cron)),
    "tag": (Tag.id, Tag.name, lambda principal: _key_gated(principal, Tag, TASKS_VIEW_KEY)),
}


def _key_gated(principal: Principal | None, entity: Any, key: str) -> Optional[Any]:
    """`select(entity)` for a principal holding `key` (or the agent principal, None), else None.

    A label names no project, so `principal.has` (held anywhere) is the right question."""
    if principal is None or principal.has(key):
        return select(entity)
    return None


def _org_admin_only(principal: Principal | None, entity: Any) -> Optional[Any]:
    """`select(entity)` for an org admin (`Principal.is_org_admin`; or the agent principal, None), else
    None — no label at all. The read seam keeps a tenant's to its own org's rows."""
    if principal is None or principal.is_org_admin:
        return select(entity)
    return None


#: `{"<noun>_id": <value>}` → noun, derived from `activity_kinds.SUBJECT_PARAMS` so the two cannot differ.
_SUBJECT_KEYS: dict[str, str] = {param: noun for param, noun in SUBJECT_PARAMS.items()}


def _subject_ref(data: Optional[dict[str, Any]]) -> Optional[tuple[str, str]]:
    """`(type, id-as-text)` for one row, or None when it names nothing.

    Resolved in order: `data.subject_ref` (capture middleware), flat `{"entity", "id"}` (machine rows),
    then the first `{"<noun>_id": …}` key in `SUBJECT_PARAMS` order (`record_write` sites). The third arm
    can name a bystander (`object_grant_auto` resolves to the grantee); the fix would be a `subject_ref`
    at that site.

    The id stays text until `_parse_subject_id`, so a malformed id still renders."""
    # The JSONB column accepts arrays/scalars; `.get` on those would 500 the page.
    if not isinstance(data, dict) or not data:
        return None
    ref = data.get("subject_ref")
    if isinstance(ref, dict):
        ref_type, ref_id = ref.get("type"), ref.get("id")
        if isinstance(ref_type, str) and ref_type and ref_id is not None:
            return ref_type, str(ref_id)
    entity, ident = data.get("entity"), data.get("id")
    if isinstance(entity, str) and entity and ident is not None:
        return entity, str(ident)
    for key, noun in _SUBJECT_KEYS.items():
        value = data.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            return noun, str(value)
    return None


#: The INTEGER range: an out-of-range parsed id would raise at the database and 500 the whole page.
_PG_INT_MIN, _PG_INT_MAX = -(2 ** 31), 2 ** 31 - 1


def _parse_subject_id(id_col: Any, raw: str) -> Any:
    """One subject id as the type its own primary key column declares, or None when it does not parse.

    Uses `id_col.type.python_type`. The payload is caller-controlled (path params of even rejected
    requests), so ints must be ASCII digits (`int` accepts `"1_0"`, whitespace and non-ASCII `Nd`
    digits, which would alias another row's label; `"²"` or >4300 digits would raise) and in range."""
    try:
        python_type = id_col.type.python_type
    except NotImplementedError:
        return None
    if python_type is int:
        if not (raw.isascii() and raw.isdigit()):
            return None
        try:
            parsed = int(raw)
        except ValueError:
            return None
        return parsed if _PG_INT_MIN <= parsed <= _PG_INT_MAX else None
    try:
        return python_type(raw)
    except (TypeError, ValueError):
        return None


def _subject_labels(
    session: Session, principal: Principal | None, refs: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Human labels for every subject on this page — one scoped `IN` query per distinct type, never per
    row. A label the caller may not read, an unparsable id or a gone row simply has no entry."""
    wanted: dict[str, dict[Any, list[str]]] = {}
    for ref_type, ref_id in refs:
        if ref_type not in _SUBJECT_MODELS:
            continue
        parsed = _parse_subject_id(_SUBJECT_MODELS[ref_type][0], ref_id)
        if parsed is not None:
            # A list per parsed value: `"42"` and `"042"` parse alike.
            wanted.setdefault(ref_type, {}).setdefault(parsed, []).append(ref_id)

    labels: dict[tuple[str, str], str] = {}
    for ref_type, by_parsed in wanted.items():
        id_col, label_col, scoped = _SUBJECT_MODELS[ref_type]
        statement = scoped(principal)
        if statement is None:
            continue
        for parsed, label in session.execute(
            statement.with_only_columns(id_col, label_col).where(id_col.in_(list(by_parsed)))
        ).all():
            if label:
                for raw in by_parsed[parsed]:
                    labels[(ref_type, raw)] = label
    return labels


# ── Labels ────────────────────────────────────────────────────────────────────────────────────────


#: Words a humanized kind spells as acronyms, never capitalized as words ("Hipaa changed").
_ACRONYMS: dict[str, str] = {"baa": "BAA", "hipaa": "HIPAA", "mfa": "MFA", "phi": "PHI", "sso": "SSO"}

#: Kinds whose humanized name misreads ("Notification unrouted" names plumbing, not what happened).
_KIND_LABELS: dict[str, str] = {
    "notification_unrouted": "Notification not delivered",
    # A person's own account write (`PUT /api/auth/me`): "Update me" reads as a request, not an act.
    "update_me": "Update own account",
}


def _humanize_kind(kind: str) -> str:
    """`hipaa_changed` → "HIPAA changed": sentence case, with `_ACRONYMS` spelled out."""
    words = kind.split("_")
    return " ".join(
        _ACRONYMS.get(word, word.capitalize() if index == 0 else word) for index, word in enumerate(words)
    )


def _row_label(kind: str, data: Optional[dict[str, Any]]) -> str:
    """The one-line human label for a row, derived SERVER-SIDE.

    `task:%` rows use `_transition_label` from `app/routes/tasks/activity.py` (one vocabulary); others humanize
    their `kind`. `_ACTIVITY_NOTE_LABELS` is not used: note rows carry a real `summary`, and widening
    the import is a named follow-up."""
    if kind.startswith("task:"):
        # `isinstance`, not `or {}`: the JSONB column accepts arrays.
        state = data.get("state") if isinstance(data, dict) else None
        return _transition_label(kind.removeprefix("task:"), state)
    if kind.startswith("task_note:"):
        return _humanize_kind(kind.removeprefix("task_note:"))
    return _KIND_LABELS.get(kind) or nodevault_label(kind) or _humanize_kind(kind)


# ── Visibility: the rows this endpoint may never serve ────────────────────────────────────────────


def _visible_clause(org_ids: frozenset[UUID] = frozenset()) -> Any:
    """The rows this page may show at all — a visibility rule, applied to the page, its estimate and
    `any_visible`: every row `row_visible` (`app/audit_visibility.py`) admits, each org transfer once
    (`_one_row_per_transfer`, told the request's `org_id` filter so a twin whose source the filter hides stays
    shown)."""
    return and_(row_visible(), _one_row_per_transfer(org_ids))


def _one_row_per_transfer(org_ids: frozenset[UUID] = frozenset()) -> Any:
    """An org transfer writes one row in EACH org it touched, sharing `data.transfer_id` (D10), so each org's
    admins see it. A reader who sees BOTH — the operator, unless their org filter (the request's, or this
    page's `org_ids`) hides the source org — gets
    the move once: the destination's twin is hidden whenever its source row still exists and is visible on this
    page (a pruned or deleted source leaves the twin showing). A tenant
    sees only its own org's row, so nothing is hidden from it. A LIST rule: the by-id read serves a twin. An edition
    without org transfer has no twins."""
    if seam.current_scope() is not seam.OPERATOR:
        return true()
    if not edition_features.enabled("org_transfer"):
        return true()
    from app.tenancy.transfer import ORG_TRANSFER_KIND

    source = aliased(ActivityEvent)
    source_kept = select(1).where(
        source.kind == ORG_TRANSFER_KIND, source.data["side"].astext == "from",
        source.data["transfer_id"].astext == ActivityEvent.data["transfer_id"].astext,
    ).exists()
    twin = and_(ActivityEvent.kind == ORG_TRANSFER_KIND, ActivityEvent.data["side"].astext == "to", source_kept)
    selected = seam.request_org_filter()
    if org_ids:
        selected = selected & org_ids if selected else org_ids
    if selected:
        twin = and_(twin, ActivityEvent.data["from_org_id"].astext.in_(sorted(str(org) for org in selected)))
    return not_(twin)


# ── The operator's filters ────────────────────────────────────────────────────────────────────────


def _require_aware(value: Optional[datetime], name: str) -> Optional[datetime]:
    """One wire timestamp as an aware UTC instant — or a 422 naming the requirement.

    A zoneless value is refused rather than assumed: `datetime-local` yields a wall clock, and reading
    it in any guessed zone silently shifts the boundary. ISO-8601 with an offset, also stated in each
    parameter's OpenAPI description."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{name}' must carry an explicit UTC offset (ISO-8601, e.g. "
                "2026-08-27T09:00:00+02:00 or 2026-08-27T07:00:00Z) — a zoneless wall clock would be "
                "read in a different zone than the timestamps this page displays"
            ),
        )
    return value.astimezone(timezone.utc)


def _like_pattern(q: str) -> str:
    """`q` as an unanchored ILIKE pattern, its LIKE metacharacters escaped with Postgres's default escape
    character, the backslash. No `ESCAPE` clause: the bare `~~*` is what `gin_trgm_ops` is matched against.
    A NUL byte in `q` 500s, as in every search needle in the tree; the fix is one shared read-side helper
    (named follow-up)."""
    needle = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{needle}%"


def _parse_user_ids(user_ids: list[str]) -> tuple[list[UUID], bool]:
    """`user_id` values as `(uuids, system)`. Malformed is a 422, not an empty page — which would read as a fact
    about a real operator."""
    uuids: list[UUID] = []
    system = False
    for raw in user_ids:
        if raw == SYSTEM_ACTOR:
            system = True
            continue
        try:
            uuids.append(UUID(raw))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'user_id' must be a uuid or the literal '{SYSTEM_ACTOR}' "
                    "(the machine rows, which have no account behind them)"
                ),
            )
    return uuids, system


def _user_clause(session: Session, user_ids: list[str]) -> Any:
    """`user_id` values, OR-combined: uuids, and `system` for the actorless rows. A uuid also reaches the
    refusals naming that user's email (actorless, but theirs: the door resolved them, or the address is theirs);
    JSONB `@>`, under `ix_activity_events_data_path`. Both spellings: a resolved refusal stores the email as the
    row holds it, an unknown address's its normalized form. Rows stay confined by `_visible_clause`, whatever the
    email read reaches."""
    uuids, system = _parse_user_ids(user_ids)
    arms = [ActivityEvent.user_id.in_(uuids)] if uuids else []
    emails = {
        spelling
        for email in session.scalars(select(AdminUser.email).where(AdminUser.id.in_(uuids))) if email
        for spelling in (email, login_lockout.normalized(email))
    } if uuids else set()
    if emails:
        arms.append(and_(
            ActivityEvent.user_id.is_(None),
            ActivityEvent.kind == SIGN_IN_REFUSAL_KIND,
            or_(*(ActivityEvent.data.contains({"identifier": email}) for email in sorted(emails))),
        ))
    if system:
        # Trimmed, as the tab reads it: a blank identifier (rows from before the writer normalized it) names nobody.
        identifier = func.nullif(func.btrim(ActivityEvent.data["identifier"].astext), "")
        names_attempt = and_(ActivityEvent.kind == SIGN_IN_REFUSAL_KIND, identifier.isnot(None))
        arms.append(and_(ActivityEvent.user_id.is_(None), not_(names_attempt)))
    return or_(*arms)


def _parse_org_ids(org_ids: list[str]) -> frozenset[UUID]:
    """The `org_id` values as uuids, or a 422 naming the requirement."""
    try:
        return frozenset(UUID(raw) for raw in org_ids)
    except ValueError:
        raise HTTPException(status_code=422, detail="'org_id' must be a uuid")


#: A subject id spelled as a JSON number too: ASCII digits, no sign, no leading zero.
_CANONICAL_DIGITS = re.compile(r"0|[1-9][0-9]*")


def _parse_subject(subject: str) -> tuple[str, str]:
    """`"<type>:<id>"` as `(type, id)`, the type one of `_SUBJECT_MODELS`; anything else is a 422."""
    ref_type, sep, ref_id = subject.partition(":")
    if not sep or not ref_id or ref_type not in _SUBJECT_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"'subject' must be '<type>:<id>' with type one of {', '.join(sorted(_SUBJECT_MODELS))}",
        )
    return ref_type, ref_id


def _subject_clause(ref_type: str, ref_id: str) -> Any:
    """The rows naming `(ref_type, ref_id)` in any of the three shapes `_subject_ref` reads, each with the id
    as a JSON string and, when canonical digits, as a JSON number (writers store both). JSONB `@>`, under
    `ix_activity_events_data_path`.

    Containment ignores `_subject_ref`'s precedence: a row with a `subject_ref` and also a `task_id` key
    matches both, though it renders the first. A filter that finds a row naming the record is the intent."""
    ids: list[Any] = [ref_id]
    if _CANONICAL_DIGITS.fullmatch(ref_id):
        ids.append(int(ref_id))
    keys = [param for param, noun in SUBJECT_PARAMS.items() if noun == ref_type]
    shapes = [
        shape
        for ident in ids
        for shape in (
            {"subject_ref": {"type": ref_type, "id": ident}},
            {"entity": ref_type, "id": ident},
            *({key: ident} for key in keys),
        )
    ]
    return or_(*(ActivityEvent.data.contains(shape) for shape in shapes))


def _filtered(
    stmt: Any,
    *,
    q: Optional[str],
    action_type: str,
    users: Optional[Any],
    org_ids: frozenset[UUID],
    status: Optional[int],
    status_class: Optional[int],
    subject: Optional[tuple[str, str]],
    start: Optional[datetime],
    end: Optional[datetime],
    console_session: Optional[str] = None,
    api_key: Optional[UUID] = None,
) -> Any:
    """Every operator-chosen filter, AND-combined (OR within one facet), applied to whichever statement it is
    handed — one helper for the page and the estimate, so `total` describes the rows paged."""
    if action_type != "all":
        stmt = stmt.where(ActivityEvent.action_type == action_type)
    if users is not None:
        stmt = stmt.where(users)
    if org_ids:
        # A narrowing on top of the seam's predicate, never instead of it: a tenant naming another org gets
        # nothing.
        stmt = stmt.where(ActivityEvent.org_id.in_(sorted(org_ids)))
    # Neither can match NULL (every non-captured row); documented in the parameter description rather than
    # inventing a status.
    if status is not None:
        stmt = stmt.where(ActivityEvent.status == status)
    if status_class is not None:
        stmt = stmt.where(ActivityEvent.status >= status_class * 100, ActivityEvent.status < status_class * 100 + 100)
    if subject is not None:
        stmt = stmt.where(_subject_clause(*subject))
    if console_session is not None:
        # JSONB `@>`, under `ix_activity_events_data_path` as `_subject_clause` is.
        stmt = stmt.where(ActivityEvent.data.contains({"console_session": console_session}))
    if api_key is not None:
        # One API key's requests (`request_caller.facts`) and its own lifecycle and refusal rows (`app/api_keys.py`).
        stmt = stmt.where(ActivityEvent.data.contains({"api_key_id": str(api_key)}))
    if start is not None:
        stmt = stmt.where(ActivityEvent.created_at >= start)
    if end is not None:
        stmt = stmt.where(ActivityEvent.created_at <= end)
    if q:
        # One expression over summary, actor, path and kind: `summary` alone misses actors and paths, and
        # transition summaries are empty. Deliberately not `data::text` (operator decision) — less a refusal's
        # attempted address, its own indexed arm, so an address finds the refusals naming it.
        pattern = _like_pattern(q)
        stmt = stmt.where(or_(
            activity_search_text().ilike(pattern),
            and_(refusal_kind_clause(), refusal_identifier_text().ilike(pattern)),
        ))
    return stmt


# ── Keyset paging and the estimate ────────────────────────────────────────────────────────────────


# The cursor (`app/keyset.py`) is `(created_at, id)`, the ORDER BY; the int4 range keeps the bind from raising.
_CURSOR_SHAPE = ["created_at", "id"]


class _Explain(Executable, ClauseElement):
    """`EXPLAIN (FORMAT JSON) <statement>` as a Core construct, so the statement's binds still go through
    their types (a JSONB containment, an expanding `IN`) — raw driver SQL would skip both."""

    inherit_cache = False

    def __init__(self, statement: Any) -> None:
        self.statement = statement


@compiles(_Explain, "postgresql")
def _compile_explain(element: _Explain, compiler: Any, **kw: Any) -> str:
    return "EXPLAIN (FORMAT JSON) " + compiler.process(element.statement, **kw)


def _capped_count(session: Session, ids: Any, known: int) -> tuple[int, Literal["exact", "at_least"]]:
    """A tenant's count: through the ORM, so the seam scopes it; one row past `_COUNT_CAP` tells "exactly the
    cap" from "more". Bounded by `_COUNT_TIMEOUT` in a savepoint, so a slow count ends as `known` rows at least
    and leaves the request's transaction usable."""
    try:
        with session.begin_nested():
            # `set_config(..., true)` is `SET LOCAL` as a select; restored after, as a released savepoint keeps it.
            before = session.scalar(select(func.current_setting("statement_timeout")))
            session.execute(select(func.set_config("statement_timeout", _COUNT_TIMEOUT, True)))
            counted = session.scalar(select(func.count()).select_from(ids.limit(_COUNT_CAP + 1).subquery()))
            session.execute(select(func.set_config("statement_timeout", before, True)))
    except OperationalError:
        log.warning("audit: a tenant's capped count passed %s; serving a floor", _COUNT_TIMEOUT)
        return known, "at_least"
    return min(counted, _COUNT_CAP), "at_least" if counted > _COUNT_CAP else "exact"


def _estimated_rows(session: Session, stmt: Any) -> int:
    """The planner's row estimate for `stmt` — a statistics read, flat in the table's size. OPERATOR SCOPE
    ONLY (the caller's check): the statistics are table-wide (`pg_stats` most-common values, histograms,
    trigram selectivity), so a tenant varying `q` or a filter could read other orgs' text off `total`.

    Run on the session's connection, so the read seam's `do_orm_execute` never sees it — under the operator
    it would add nothing but the operator's own org filter, which `criteria_for` supplies here by hand."""
    criteria = seam.criteria_for(ActivityEvent, seam.current_scope())
    if criteria is not None:
        stmt = stmt.where(criteria)
    plan = session.connection().execute(_Explain(stmt)).scalar_one()
    if isinstance(plan, str):
        plan = json.loads(plan)
    return int(plan[0]["Plan"]["Plan Rows"])


# ── Wire shapes ───────────────────────────────────────────────────────────────────────────────────


class AuditSubjectOut(BaseModel):
    """What a row happened to. `label` is None for a gone row or an unresolvable type; type and id are
    still served."""

    type: str
    id: str
    label: Optional[str] = None


class AuditOrgOut(UuidRefOut):
    """The org a row belongs to."""


class AuditEventOut(BaseModel):
    """One row of the audit trail.

    Captured rows have `status` and `path` and a handler `kind`; machine rows have null
    `status`/`path`/`user_id` and a `task:`/`task_note:` kind."""

    id: int
    created_at: datetime
    user_id: Optional[UUID] = None
    actor_label: Optional[str] = None
    action_type: str
    kind: str
    label: str
    summary: str
    path: Optional[str] = None
    method: Optional[str] = None
    status: Optional[int] = None
    data: dict[str, Any] = Field(default_factory=dict)
    subject: Optional[AuditSubjectOut] = None
    #: The row's org — served to the operator alone (a tenant sees only its own); null otherwise.
    org: Optional[AuditOrgOut] = None


class AuditFilterLabelsOut(BaseModel):
    """Names for the ids in the request's own `user_id`/`org_id`/`subject`, so a chip restored from a URL
    reads as a name. An id with no name the caller may read has no entry."""

    users: dict[str, str] = Field(default_factory=dict)
    orgs: dict[str, str] = Field(default_factory=dict)
    subject: Optional[str] = None


class AuditPageOut(BaseModel):
    """One keyset page. `next_cursor` is null on the last page. `total_kind` says what `total` is: `exact`
    (an uncursored page holding every match, or a tenant's count within `_COUNT_CAP`), `estimate` (the
    operator's planner figure, never below the rows this read proved exist) or `at_least` (a tenant's count
    stopped at the cap, `total` the cap; or ran past `_COUNT_TIMEOUT`, `total` the rows already proved). `any_visible` separates "nothing matches" from "nothing captured
    yet"."""

    items: list[AuditEventOut]
    next_cursor: Optional[str] = None
    total: int
    total_kind: Literal["exact", "estimate", "at_least"]
    any_visible: bool
    filter_labels: AuditFilterLabelsOut


class AuditUserSuggestion(BaseModel):
    """A user facet value. `id` is a uuid as text, or the `system` sentinel."""

    id: str
    label: str
    hint: Optional[str] = None


class AuditOrgSuggestion(BaseModel):
    id: str
    name: str
    slug: str


class AuditSubjectSuggestion(BaseModel):
    """A record facet value; `type:id` is what the `subject` parameter takes."""

    type: str
    id: str
    label: str


class AuditSuggestOut(BaseModel):
    users: list[AuditUserSuggestion]
    orgs: list[AuditOrgSuggestion]
    subjects: list[AuditSubjectSuggestion]


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=AuditPageOut)
@feature("audit", "view")
def browse_audit(
    q: Optional[str] = Query(
        default=None,
        max_length=_Q_MAX_LEN,
        description=(
            "Case-insensitive substring match across summary, actor label, path and kind — and a refused "
            "sign-in's attempted address — at least 3 characters. Deliberately NOT over the captured request "
            "payload."
        ),
    ),
    action_type: str = Query(
        default="write",
        description="all | read | write. Defaults to write — the mutations, which is what an audit reader came for.",
    ),
    user_id: list[str] = Query(
        default=[],
        description=(
            "Repeatable (at most 50), OR-combined: a uuid (with the refused sign-ins naming that user's email), "
            "or the literal 'system' for the machine rows (user_id IS NULL, less refused sign-ins that name an "
            "attempted identifier)."
        ),
    ),
    # `None`, not `[]`: absence is how a read filter spells "do not narrow by org"
    # (`test_tenant_create_surfaces.py::_org_id_parameter_defaults`).
    org_id: Optional[list[str]] = Query(
        default=None,
        description=(
            "Repeatable (at most 50), OR-combined org uuids. Narrows within the orgs the caller may read; "
            "another org matches nothing."
        ),
    ),
    status: Optional[int] = Query(
        default=None,
        # Bounded to the HTTP range: the column is `SmallInteger`, and an out-of-range bind would 500.
        ge=100,
        le=599,
        description=(
            "HTTP status of the captured request, 100..599. EXCLUDES every row whose status is NULL, which is "
            "every row that does not describe an HTTP response: all read rows, all task:* and "
            "task_note:* machine rows, and the surviving hand-written write-audit rows. This is not a "
            "gap to be worked around — those rows have no status to match — but a client MUST disclose "
            "it, because selecting a status silently narrows the trail to captured requests only. "
            "CLEARED MEANS OMITTED: drop the key, do not send it empty — '?status=' is a 422. Not with "
            "status_class."
        ),
    ),
    status_class: Optional[int] = Query(
        default=None,
        ge=1,
        le=5,
        description=(
            "1..5: every status in that hundred (4 is 400..499). EXCLUDES NULL-status rows exactly as status "
            "does. CLEARED MEANS OMITTED. Not with status."
        ),
    ),
    subject: Optional[str] = Query(
        default=None,
        max_length=_Q_MAX_LEN,
        description="'<type>:<id>' (e.g. 'task:42'): only rows naming that record.",
    ),
    start: Optional[datetime] = Query(
        default=None,
        description=(
            "Inclusive lower bound on created_at. ISO-8601 WITH AN EXPLICIT OFFSET (…+02:00 or …Z); a "
            "zoneless value is refused with 422 rather than read in a guessed zone. CLEARED MEANS "
            "OMITTED: drop the key, do not send it empty."
        ),
    ),
    end: Optional[datetime] = Query(
        default=None,
        description=(
            "Inclusive upper bound on created_at. Same explicit-offset and cleared-means-omitted "
            "requirements as start."
        ),
    ),
    console_session: Optional[str] = Query(
        default=None,
        pattern=request_caller.CONSOLE_SESSION.pattern,
        description=(
            "One browser sign-in's id (16 hex), as data.console_session: only that session's captured requests and "
            "the task transitions they caused. Hand-written write records, cron and worker rows, and rows written "
            "before per-session capture existed carry none."
        ),
    ),
    api_key: Optional[UUID] = Query(
        default=None,
        description=(
            "One API key's id, as data.api_key_id: the requests made with it and its own created, edited, rotated, "
            "revoked and refused records."
        ),
    ),
    cursor: Optional[str] = Query(
        default=None,
        max_length=_Q_MAX_LEN,
        description="The previous page's next_cursor, unchanged. Omitted means the newest page.",
    ),
    limit: int = Query(default=50, ge=1, le=_PER_MAX),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AuditPageOut:
    """One page of the console's activity trail, newest first, with a count that stays cheap at any size.

    Gated `audit:view` (separately grantable from `GET /api/command-log`'s `system:view`). Flat within
    the caller's org; see the module docstring.

    THE STATUS FILTER EXCLUDES EVERY ROW WHOSE STATUS IS NULL — reads, machine rows, hand-written write
    audits (including the superadmin-bypass audit). Correct, but the Activity tab must disclose it. So does
    `status_class`.

    Keyset paged on `created_at DESC, id DESC` (a total order): `cursor` is the last row served, and the next
    page is the rows strictly older, so rows arriving at the head never shift a page (no offset, no pin).

    `total`: when an uncursored page is also the last, it IS the count. Otherwise, for the operator, the
    planner's estimate of the filtered set (`_estimated_rows`), floored at the rows this read proved exist —
    a `count(*)` is linear in the table, so none is run; for anyone else a seam-scoped count that
    stops at `_COUNT_CAP` (the estimate would leak other orgs' statistics). `any_visible` is one indexed
    probe, skipped when the page has rows.
    """
    plan_terms.require_tenant_switch(session, principal, plan_terms.AUDIT)
    # An empty string is a cleared control, not a filter (browsers send `user_id=`). Typed parameters
    # are not normalised — they declare no `BeforeValidator`, so pydantic refuses `""` before this body
    # runs — and a cleared typed control must drop its key (`?status=` is a pinned 422).
    action_type = action_type or "write"
    q = q or None
    if q is not None and len(q) < _Q_MIN_LEN:
        raise HTTPException(status_code=422, detail=f"search text needs at least {_Q_MIN_LEN} characters")
    user_ids = [value for value in user_id if value]
    org_values = [value for value in org_id or () if value]
    for name, given in (("user_id", user_ids), ("org_id", org_values)):
        if len(given) > _FACET_MAX:
            raise HTTPException(status_code=422, detail=f"at most {_FACET_MAX} '{name}' values per request")
    org_ids = _parse_org_ids(org_values)
    if action_type not in _ACTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown action_type '{action_type}' — expected one of {', '.join(_ACTION_TYPES)}",
        )
    if status is not None and status_class is not None:
        raise HTTPException(status_code=422, detail="send 'status' or 'status_class', not both")
    subject_ref = _parse_subject(subject) if subject else None
    after = keyset.decode_cursor(
        cursor, _CURSOR_SHAPE, is_stamp=lambda name: name == "created_at", int_range=(1, 2**31 - 1), status=422,
        detail="'cursor' is not one this listing issued — send back a next_cursor unchanged, or leave it out",
    ) if cursor else None
    start = _require_aware(start, "start")
    end = _require_aware(end, "end")

    visible = _visible_clause(org_ids)
    users = _user_clause(session, user_ids) if user_ids else None

    def filtered(stmt: Any) -> Any:
        return _filtered(
            stmt.where(visible),
            q=q, action_type=action_type, users=users, org_ids=org_ids, status=status,
            status_class=status_class, subject=subject_ref, start=start, end=end,
            console_session=console_session,
            api_key=api_key,
        )

    page = filtered(select(ActivityEvent))
    if after is not None:
        page = page.where(tuple_(ActivityEvent.created_at, ActivityEvent.id) < tuple_(*after))
    # One row past the page answers "is there another" without a count.
    rows = list(session.scalars(
        page.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc()).limit(limit + 1)
    ).all())
    more = len(rows) > limit
    rows = rows[:limit]

    total_kind: Literal["exact", "estimate", "at_least"]
    if after is None and not more:
        total, total_kind = len(rows), "exact"
    elif seam.current_scope() is seam.OPERATOR:
        known = len(rows) + int(more)
        total, total_kind = max(_estimated_rows(session, filtered(select(ActivityEvent.id))), known), "estimate"
    else:
        total, total_kind = _capped_count(session, filtered(select(ActivityEvent.id)), len(rows) + int(more))
    any_visible = bool(rows) or session.scalar(
        select(ActivityEvent.id).where(_visible_clause()).limit(1)
    ) is not None

    return AuditPageOut(
        items=_events_out(session, principal, user, rows),
        next_cursor=keyset.encode_cursor(_CURSOR_SHAPE, [rows[-1].created_at, rows[-1].id]) if more else None,
        total=total,
        total_kind=total_kind,
        any_visible=any_visible,
        filter_labels=_filter_labels(session, principal, user_ids, org_ids, subject_ref),
    )


def _filter_labels(
    session: Session,
    principal: Principal | None,
    user_ids: list[str],
    org_ids: frozenset[UUID],
    subject: Optional[tuple[str, str]],
) -> AuditFilterLabelsOut:
    """Names for the request's own filter values — at most one read per facet, none for an empty one.

    A user is named by the newest `actor_label` the trail holds for them (as the trail itself renders
    them; a `LATERAL` top-1 per id through `ix_activity_events_actor_newest`), else their email — only if
    the trail names them at all (`_in_trail`), so a uuid probe cannot read a roster. Org names
    are the operator's alone; the subject's label is the scoped one (`_subject_labels`)."""
    labels = AuditFilterLabelsOut()
    uuids = sorted({UUID(raw) for raw in user_ids if raw != SYSTEM_ACTOR})
    if SYSTEM_ACTOR in user_ids:
        labels.users[SYSTEM_ACTOR] = SYSTEM_ACTOR_LABEL
    if uuids:
        wanted = values(column("user_id", PG_UUID(as_uuid=True)), name="wanted").data([(u,) for u in uuids])
        newest = lateral(
            select(ActivityEvent.actor_label)
            .where(ActivityEvent.user_id == wanted.c.user_id, row_visible())
            .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
            .limit(1)
        )
        for ident, label in session.execute(
            select(wanted.c.user_id, newest.c.actor_label).select_from(wanted).join(newest, true())
        ).all():
            if label:
                labels.users[str(ident)] = label
        unnamed = [ident for ident in uuids if str(ident) not in labels.users]
        if unnamed:
            for ident, email in session.execute(
                select(AdminUser.id, AdminUser.email).where(AdminUser.id.in_(unnamed), _in_trail())
            ).all():
                labels.users[str(ident)] = email
    if org_ids and seam.current_scope() is seam.OPERATOR:
        for ident, name in session.execute(select(Org.id, Org.name).where(Org.id.in_(sorted(org_ids)))).all():
            labels.orgs[str(ident)] = name
    if subject is not None:
        labels.subject = _subject_labels(session, principal, [subject]).get(subject)
    return labels


@router.get("/events/{event_id}", response_model=AuditEventOut)
@feature("audit", "view")
def get_audit_event(
    event_id: int,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AuditEventOut:
    """One row of the trail by id, in the list's item shape — so a pasted row URL opens the same record.

    Under the list's visibility (`audit_visibility.row_visible`, and the tenancy seam's org scope), so another
    org's row or a hidden one is a 404 like a missing id. The transfer-twin de-dup is the list's presentation rule, not
    visibility: a twin the caller may see is served."""
    plan_terms.require_tenant_switch(session, principal, plan_terms.AUDIT)
    # `activity_events.id` is int4: an id past it would fail the bind, and names no row anyway.
    row = None if not 0 < event_id < 2**31 else session.scalar(
        select(ActivityEvent).where(ActivityEvent.id == event_id, row_visible())
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"no audit event with id {event_id}")
    return _events_out(session, principal, user, [row])[0]


def _unattributed_sign_ins(session: Session, rows: list[ActivityEvent]) -> set[int]:
    """The ids of `rows` that are sign-in security records resolving no user: filed under
    `UNATTRIBUTED_SIGN_IN_ORG_ID` only because no org owns them, so they serve no org rather than read as the operator
    org. A refusal of an operator-org user files there too, named by its email; one read of those emails tells the
    two apart, with no field on the row."""
    candidates = {
        row.id: login_lockout.normalized(identifier) if isinstance(identifier, str) else ""
        for row in rows
        if row.org_id == UNATTRIBUTED_SIGN_IN_ORG_ID and row.user_id is None and row.kind in _SIGN_IN_SECURITY_KINDS
        for identifier in [(row.data or {}).get("identifier")]
    }
    addresses = {address for address in candidates.values() if address}
    known = set(session.scalars(
        select(func.lower(AdminUser.email)).where(
            AdminUser.org_id == UNATTRIBUTED_SIGN_IN_ORG_ID, func.lower(AdminUser.email).in_(addresses),
        )
    )) if addresses else set()
    return {row_id for row_id, address in candidates.items() if address not in known}


def _events_out(
    session: Session, principal: Principal | None, user: AdminUser, rows: list[ActivityEvent]
) -> list[AuditEventOut]:
    """`rows` as the wire serves them — the one serializer for the page and the by-id read."""
    refs = [(row, _subject_ref(row.data)) for row in rows]
    labels = _subject_labels(session, principal, [ref for _row, ref in refs if ref is not None])
    # One grant read for the whole page.
    allowed_accounts = granted_account_slugs(session, principal)
    # One tenant verdict for the page, from the reader's org.
    tenant = is_tenant_org(user.org_id)
    # One `IN` read of org names, for the operator alone: a tenant's rows are all its own.
    org_names: dict[UUID, str] = {}
    if rows and seam.current_scope() is seam.OPERATOR:
        org_names = dict(session.execute(
            select(Org.id, Org.name).where(Org.id.in_({row.org_id for row in rows}))
        ).all())
    unattributed = _unattributed_sign_ins(session, rows) if org_names else set()
    return [
        from_row(
            AuditEventOut, row,
            label=_row_label(row.kind, row.data),
            data=tenant_worded(visible_payload(row.data, allowed_accounts), tenant),
            subject=(
                None if ref is None
                else AuditSubjectOut(type=ref[0], id=ref[1], label=labels.get(ref))
            ),
            org=(
                AuditOrgOut(id=row.org_id, name=org_names[row.org_id])
                if row.org_id in org_names and row.id not in unattributed else None
            ),
        )
        for row, ref in refs
    ]


def _in_trail() -> Any:
    """`AdminUser` rows the caller's trail names as an actor — an `EXISTS` through the ORM, so the seam
    scopes it to the orgs the caller may read, served by `ix_activity_events_actor_newest`. The user facet
    offers people who did something here, never the member roster (`audit:view` is not `users:view`)."""
    return select(1).where(ActivityEvent.user_id == AdminUser.id).exists()


#: The record types the search box offers, in suggestion order.
_SUGGESTED_SUBJECTS = ("task", "project", "node", "credential")

#: `#123` or `123`: a task number. Nine digits at most, inside `INTEGER`.
_TASK_NUMBER = re.compile(r"#?([1-9][0-9]{0,8})")


@router.get("/suggest", response_model=AuditSuggestOut)
@feature("audit", "view")
def audit_suggest(
    q: str = Query(min_length=1, max_length=_Q_MAX_LEN, description="What the person has typed so far."),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AuditSuggestOut:
    """The search box's facet suggestions for `q`: users, orgs and records, at most `_SUGGEST_MAX` each.

    Users are the seam-scoped members whose email or name matches and whom the trail names (`_in_trail`),
    led by System when `q` starts its name;
    under the operator each hint names the member's org, since one person holds a seat per org. Orgs are
    the operator's alone (empty otherwise) and ignore the operator's org filter: picking one is how they
    narrow. Records go through each type's own scoped select (`_SUBJECT_MODELS`), so a record the caller
    may not read is never offered; a task also matches by number. Every arm is a bounded indexed or
    small-table read, not a walk of the trail."""
    plan_terms.require_tenant_switch(session, principal, plan_terms.AUDIT)
    pattern = _like_pattern(q)
    operator = seam.current_scope() is seam.OPERATOR

    users: list[AuditUserSuggestion] = []
    if SYSTEM_ACTOR.startswith(q.lower()):
        users.append(AuditUserSuggestion(id=SYSTEM_ACTOR, label=SYSTEM_ACTOR_LABEL))
    # The person's label (`identities.label_expr`): the name pair, else the email — so the hint below is
    # the pair alone, never the address twice.
    label = label_expr()
    members = session.execute(
        select(AdminUser.id, AdminUser.email, label, AdminUser.org_id)
        .where(
            AdminUser.is_system.is_(False),
            or_(AdminUser.email.ilike(pattern), label.ilike(pattern)),
            _in_trail(),
        )
        .order_by(AdminUser.email, AdminUser.id)
        .limit(_SUGGEST_MAX - len(users))
    ).all()
    seat_orgs: dict[UUID, str] = {}
    if operator and members:
        seat_orgs = dict(session.execute(
            select(Org.id, Org.name).where(Org.id.in_({org for *_rest, org in members}))
        ).all())
    for ident, email, name, org in members:
        hint = name if name and name != email else None
        if org in seat_orgs:
            hint = f"{hint} · {seat_orgs[org]}" if hint else seat_orgs[org]
        users.append(AuditUserSuggestion(id=str(ident), label=email, hint=hint))

    orgs: list[AuditOrgSuggestion] = []
    if operator:
        orgs = [
            AuditOrgSuggestion(id=str(ident), name=name, slug=slug)
            for ident, name, slug in session.execute(
                select(Org.id, Org.name, Org.slug)
                .where(or_(Org.name.ilike(pattern), Org.slug.ilike(pattern)))
                .order_by(Org.name, Org.id)
                .limit(_SUGGEST_MAX)
            ).all()
        ]

    return AuditSuggestOut(users=users, orgs=orgs, subjects=_subject_suggestions(session, principal, q, pattern))


def _subject_suggestions(
    session: Session, principal: Principal | None, q: str, pattern: str
) -> list[AuditSubjectSuggestion]:
    """Records whose label matches, one scoped read per type, interleaved so one busy type cannot crowd out
    the rest; a task named by number comes first."""
    per_type: list[list[AuditSubjectSuggestion]] = []
    number = _TASK_NUMBER.fullmatch(q.strip())
    for ref_type in _SUGGESTED_SUBJECTS:
        id_col, label_col, scoped = _SUBJECT_MODELS[ref_type]
        statement = scoped(principal)
        if statement is None:
            continue
        match = label_col.ilike(pattern)
        first = None
        if ref_type == "task" and number is not None:
            first = id_col == int(number[1])
            match = or_(first, match)
        found = session.execute(
            statement.with_only_columns(id_col, label_col)
            .where(match)
            .order_by(*([case((first, 0), else_=1)] if first is not None else []), label_col, id_col)
            .limit(_SUGGEST_MAX)
        ).all()
        per_type.append([
            AuditSubjectSuggestion(type=ref_type, id=str(ident), label=label) for ident, label in found if label
        ])
    interleaved = [item for item in chain.from_iterable(zip_longest(*per_type)) if item is not None]
    return interleaved[:_SUGGEST_MAX]
