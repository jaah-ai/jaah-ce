"""Task listing: search, the board, list, facets, tree, counts and the persona registry."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Sequence
from typing import Any, Literal, NamedTuple, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, literal, or_, select, text
from sqlalchemy.orm import Session

from app import actors, edition_features, model_catalog
from app.trackers import project_links
from app.clock import as_utc
from app.auth import adopt_user, require_user
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.authz.visibility import (
    board_draft_clause,
    scoped_task_select,
    visible_epic_children,
    unhidden_task_clause,
    visible_task_clause,
)
from app.db_async import on_session
from app.entity_shape import SYSTEM_ACTOR_ID, from_row, optional_uuid
from app.list_page import like_pattern
from app.agent_sessions import STATUS_RUNNING as SESSION_RUNNING
from app.models.agent_sessions import AgentSession
from app.models.identity import AdminUser
from app.models.task_extras import TaskTag
from app.models.tasks import Task, task_id_text, task_search_text
from app.identities import display_name, label_expr
from app.persona_registry import PERSONA_PICKER_HINT, PERSONA_RECORDS, PROJECT_DEFAULT_PERSONAS, TASK_CREATION_LICENCE
from app import runner_places, slot_allocation, task_prs
from app import keyset, preferences, task_sections
from app import merge_lane_state, task_tree
from app.task_sections import Section
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, READY, _FOCUS_TOP_LEVEL_STATES
from app import task_done_notice, task_source
from app.task_run_metrics import run_aggregates
from app.task_ratings import rating_aggregates
from app.task_tags import active_tag_ids_select, tag_refs_for
from app.task_title import draft_title
from app.tenancy.copy import WITHHELD_ACTOR
from app.tenancy.labels import org_refs
from app.routes.tasks.policy import _ORPHAN_CHILD_PERSONAS
from app.routes.tasks.task_wire import (
    NO_DELIVERY_FAILURE,
    TaskListOut,
    TaskOut,
    _answer_delivering_ids,
    _answer_delivery_failures,
    _asking_task_keys,
    _authored_draft_ids,
    _child_counts,
    _gated_child_ids,
    _list_placements,
    _may_edit_ids,
    _org_wait_reasons,
    _task_out,
    _unmet_prerequisite_counts,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ── Top-level list bound + ordering ─────────────────────────────────────────────────────────────────
# `list_tasks`'s top-level branch orders unfinished-first, then most-recently-updated (`id desc` tiebreak),
# clamped to this many rows; the console list lazy-loads on scroll.
TASK_LIST_MAX_LIMIT = 500


def _clamp_task_list_limit(limit: Optional[int]) -> int:
    """Page-size clamp: `None` -> the max (never unbounded); a value is floored at 1, capped at the max."""
    if limit is None:
        return TASK_LIST_MAX_LIMIT
    return max(1, min(limit, TASK_LIST_MAX_LIMIT))


# ── The NUMBER a search term names, when it names one ───────────────────────────────────────────────
# Single home for `?q=`'s number arm: `42`, `#42`, `task #42`, whitespace optional; leading zeros
# collapse. `[0-9]` not `\d` (non-ASCII digits). Nine digits because `tasks.id` is int4. `(?:#\s*)?`
# not `#?\s*`: two competing `\s*` are O(n²) on a whitespace run, on the single API process.
_TASK_NUMBER_TERM = re.compile(r"^(?:task\s*)?(?:#\s*)?([0-9]{1,9})$", re.IGNORECASE)

# `task #` plus nine digits is 15 chars; generous room for whitespace. The structural half of the O(n²) fix.
_MAX_NUMBER_TERM_LEN = 32


def _task_number_term(term: str) -> Optional[int]:
    """The task NUMBER a `?q=` term names, or `None` (ordinary free text). Purely lexical. A number
    WIDENS the text match by a task-id match, never replaces it."""
    stripped = term.strip()
    # Length refused BEFORE the pattern runs, so no term's cost depends on input length.
    if len(stripped) > _MAX_NUMBER_TERM_LEN:
        return None
    match = _TASK_NUMBER_TERM.match(stripped)
    return int(match.group(1)) if match else None


# ── The task a pasted console LINK names ──────────────────────────────────────────────────────────────
# A console task link (`/tasks/<id>`) names exactly one task, so it matches that row alone (never the partial
# number arm). Jaah's own console hosts only (operator decision); scheme optional; a trailing
# `/`, `?query` or `#fragment` is the browser's, not the operator's. No nested quantifier, and the length
# is refused first, as for the number arm.
_TASK_LINK_TERM = re.compile(
    r"^(?:https?://)?(?:app|dev)\.jaah\.ai/tasks/([0-9]{1,9})/?(?:[?#]\S*)?$", re.IGNORECASE
)
_MAX_LINK_TERM_LEN = 512


def _task_link_term(term: str) -> Optional[int]:
    """The task a `?q=` term LINKS to (a Jaah console task URL), or `None`. Purely lexical."""
    stripped = term.strip()
    if len(stripped) > _MAX_LINK_TERM_LEN:
        return None
    match = _TASK_LINK_TERM.match(stripped)
    return int(match.group(1)) if match else None


def _task_number_match(number: int) -> Any:
    """Predicate: this row's id CONTAINS the digits of `number` — PARTIAL (`196` finds `1964`), via TEXT
    `task_id_text()` + LIKE. No escaping needed: `number` is ASCII digits only. One home because `list_tasks` uses it
    in both `WHERE` and `ORDER BY`, and the two must describe the same row."""
    pattern = f"%{number}%"
    return task_id_text().like(pattern)


# ── Full-text search over title, description and outcome ───────────────────────────────────────────
# Every word of `?q=` must appear, in any order, as a case-insensitive LITERAL substring (`like_pattern`
# escapes LIKE's wildcards with `\`, Postgres's default ESCAPE, so the clause stays a bare `ILIKE` the
# trigram index serves) of `task_search_text()`. Words past the cap are dropped: each is one more index probe.
_MAX_SEARCH_WORDS = 16


def task_search_clause(q: str) -> tuple[Any, Optional[int]]:
    """`?q=`'s predicate for a non-blank `q`, and the task number it names (`None` for none). One home for
    the board and the agent peer search, so both mean the same thing by a search. A task LINK is an exact
    match on the task it names, nothing else."""
    linked = _task_link_term(q)
    if linked is not None:
        return Task.id == linked, linked
    # Deduped case-insensitively, searched as typed: `casefold` rewrites (`ß` → `ss`) where `ILIKE` does not.
    words = list({word.casefold(): word for word in reversed(q.split())}.values())[::-1][:_MAX_SEARCH_WORDS]
    search_text = task_search_text()
    text_match = and_(*(search_text.ilike(like_pattern(word)) for word in words))
    number = _task_number_term(q)
    return (text_match if number is None else or_(text_match, _task_number_match(number))), number


# ── `updated_at` window shared by the list route and its tag-count mirror ───────────────────────────
# One home, so the list and `task_tag_counts` apply the same edge rule. Both bounds INCLUSIVE, independent.
# A NAIVE bound is read as UTC (`as_utc`) rather than 422ed: left naive, Postgres would cast it with the
# SESSION `TimeZone`, shifting the window on a server setting no caller can see.
def _updated_at_window(
    updated_after: Optional[datetime], updated_before: Optional[datetime]
) -> list[Any]:
    """The `updated_at >= after` / `<= before` predicates for the bounds supplied; `[]` for neither, so
    callers splat it unconditionally."""
    window: list[Any] = []
    if updated_after is not None:
        window.append(Task.updated_at >= as_utc(updated_after))
    if updated_before is not None:
        window.append(Task.updated_at <= as_utc(updated_before))
    return window


# `list_tasks`'s one sort key: 0 = still actionable, 1 = finished business. An archived row is never
# unfinished; a hidden row is finished business even while running (hidden rows outnumber visible ~4:1).
# A hidden Runner-cron row the operator chose to show also sorts as finished — accepted: `hidden AND not
# live` would lift every running system row, and reading `persona` here is forbidden (P3b;
# `models/tasks.py:Task.hidden`). It orders the route, so the web pickers reading this bounded list lose
# finished rows past `?limit=`.
_TASK_LIST_COMPLETED_GROUP = case(
    (Task.state == DONE, 1),
    (Task.archived_at.isnot(None), 1),
    # A hidden row asking a question is unfinished work somebody owes an answer (`unhidden_task_clause`).
    (and_(Task.hidden.is_(True), Task.state != AWAITING_RESPONSE), 1),
    else_=0,
)


def _feature_filter(raw: str):
    """The `GET /api/tasks?feature_id=` predicate: a feature's id → its rows, else 422.
    The access clause is untouched: a feature the caller cannot see simply matches no row they can."""
    feature_id = optional_uuid(raw)
    if feature_id is None:
        raise HTTPException(status_code=422, detail="feature_id must be a feature's id")
    return Task.feature_id == feature_id


def _exclusion_clauses(
    exclude_project_id: Optional[list[int]],
    exclude_feature_id: Optional[list[UUID]],
) -> list[Any]:
    """The board's `exclude_project_id=` / `exclude_feature_id=` predicates: drop the named projects' rows and the
    named features' rows (a row filed in its project itself is never dropped by the second). The web sends a user's MUTED nodes above the selected scope
    (`lib/taskMute.ts`); the server holds no mute rule of its own. Truthiness, as `project_id`: an empty list
    would compile to `NOT IN ()`."""
    clauses: list[Any] = []
    if exclude_project_id:
        clauses.append(Task.project_id.not_in(sorted(exclude_project_id)))
    if exclude_feature_id:
        clauses.append(or_(Task.feature_id.is_(None), Task.feature_id.not_in(sorted(exclude_feature_id))))
    return clauses


def board_row_clauses(
    principal: Principal | None, *, include_hidden: bool, include_archived: bool, persona: Optional[str]
) -> list[Any]:
    """THE board's top-level-row predicate — which rows the task board is made of before any narrowing
    filter: top-level only, other people's drafts per `board_draft_clause`, `hidden` rows unless opted in,
    archived rows unless opted in, and the parentless child-only personas unless `persona` names one (then
    only that persona). One home, reused by every route that must describe the same board."""
    clauses: list[Any] = [Task.parent_task_id.is_(None), board_draft_clause(principal, include_hidden=include_hidden)]
    if not include_hidden:
        # Sole UI-filter predicate (P3b/P3c): reads `hidden` (and the open-question exception), never `persona`.
        clauses.append(unhidden_task_clause())
    if not include_archived:
        # The archived half of the Filter panel's one opt-in (the tick sends this and `include_hidden`
        # together; the params stay independent). The top-level counterpart of the archived-children exclusion
        # in `visible_epic_children`/`_epic_children`.
        clauses.append(Task.archived_at.is_(None))
    if persona is not None:
        # An explicit persona request opts back into the orphan-child personas the default view hides.
        clauses.append(Task.persona == persona)
    else:
        # No NULL arm: the column is NOT NULL, so a plain NOT IN can never drop a row to UNKNOWN.
        clauses.append(~Task.persona.in_(_ORPHAN_CHILD_PERSONAS))
    return clauses


def _source_clauses(source: Optional[list[str]]) -> list:
    """The origin filter, a union of `app/task_source.py` codes. An unknown code is not a filter (a
    hand-edited URL widens, never empties the board), and none left means no clause — `IN ()` would."""
    known = sorted(set(source or ()) & set(task_source.SOURCES))
    return [Task.source.in_(known)] if known else []


def _creator_clauses(created_by_id: Optional[list[UUID]]) -> list:
    """The creator filter, a union of creator ids, by id never by the `created_by` label (names can collide or
    change). `SYSTEM_ACTOR_ID` is the roster's "System" entry (`task_creators`): it also matches rows with NO
    recorded creator (a NULL `created_by_id`), so it means "no human filed this". Truthiness: an empty list would
    compile to `IN ()` and empty the board."""
    if not created_by_id:
        return []
    ids = sorted(set(created_by_id))
    by_id = Task.created_by_id.in_(ids)
    return [or_(by_id, Task.created_by_id.is_(None)) if SYSTEM_ACTOR_ID in ids else by_id]


def board_filtered_select(
    principal: Principal | None,
    *,
    state: Optional[str],
    project_id: Optional[list[int]],
    feature_id: Optional[str],
    created_by_id: Optional[list[UUID]],
    persona: Optional[str],
    q: Optional[str],
    include_hidden: bool,
    include_archived: bool,
    exclude_done: bool,
    focus: bool,
    updated_after: Optional[datetime],
    updated_before: Optional[datetime],
    exclude_project_id: Optional[list[int]] = None,
    exclude_feature_id: Optional[list[UUID]] = None,
    source: Optional[list[str]] = None,
    assigned_to_id: Optional[list[UUID]] = None,
) -> tuple[Select, Optional[int]]:
    """`GET /api/tasks`' top-level row set: the caller's visible board rows narrowed by every list filter
    but `section`, `limit` and `after`, plus the task number `q` names (it orders the page). One home, so
    `GET /api/tasks/counts` counts exactly the rows the list pages through."""
    stmt = scoped_task_select(principal).where(
        *board_row_clauses(principal, include_hidden=include_hidden, include_archived=include_archived, persona=persona)
    )
    if state is not None:
        stmt = stmt.where(Task.state == state)
    if focus:
        # The focus predicate: an allow-list, `_FOCUS_TOP_LEVEL_STATES` (app/task_lifecycle.py), so a new state
        # must be classified there. `sorted`, as in `authz/visibility.py::scope_key_clause`: per-process `str` hashing
        # would vary bind order between workers and cost statement-cache hits.
        stmt = stmt.where(Task.state.in_(sorted(_FOCUS_TOP_LEVEL_STATES)))
        # Focus overrules `include_archived` (the panel's one tick sends both), but not `include_hidden`: the ask
        # names archived rows as distractions and says nothing about system rows.
        stmt = stmt.where(Task.archived_at.is_(None))
    # Full-text search (`task_search_clause`), widened by the row's number when the term names one. A blank
    # `q` is ignored. The number arm is additive and partial (`196` matches #1964 and any text with those
    # digits), so no previous result is lost.
    number: Optional[int] = None
    if q is not None and q.strip():
        match, number = task_search_clause(q)
        stmt = stmt.where(match)
    if exclude_done:
        # For the prerequisite picker (`components/tasks/TaskDependencies.tsx`); server-side, since a
        # client-side filter over a clamped page would drop candidates past `?limit=`. The task the term
        # NAMES (by number or link) stays offerable even when done (operator decision).
        not_done = Task.state != DONE
        stmt = stmt.where(not_done if number is None else or_(not_done, Task.id == number))
    # Truthiness, as for `created_by_id`: an empty list would compile to `IN ()` and empty the board.
    if project_id:
        stmt = stmt.where(Task.project_id.in_(sorted(project_id)))
    if feature_id is not None:
        stmt = stmt.where(_feature_filter(feature_id))
    stmt = stmt.where(*_exclusion_clauses(exclude_project_id, exclude_feature_id))
    stmt = stmt.where(*_creator_clauses(created_by_id))
    # The assignee filter (the board's avatar strip), by id on `created_by_id`'s reasoning.
    if assigned_to_id:
        stmt = stmt.where(Task.assigned_to_id.in_(assigned_to_id))
    stmt = stmt.where(*_source_clauses(source))
    # The date-range filter in SQL, since the page is a clamped window. `task_tag_counts` applies the same
    # window from the same helper.
    stmt = stmt.where(*_updated_at_window(updated_after, updated_before))
    return stmt, number


# ── Keyset paging for `GET /api/tasks` ────────────────────────────────────────────────────────────────
# Cursor codec: `app/keyset.py`; another `section`, focus on/off, a numbered `q` or none is another
# ordering. Keys are live: a row whose sort key moves between pages may repeat or be skipped.
_NEXT_CURSOR_HEADER = "X-Next-Cursor"

# `GET /api/tasks?order=`: absent is the recency order; `section` puts the State board's screen order first.
BoardOrder = Literal["section"]


def child_rows_out(session: Session, principal: Principal | None, rows: list[Task]) -> list[TaskOut]:
    """`GET /api/tasks?parent_id=`'s rows: `rows` (a parent's children, already narrowed for the caller) with every
    per-row read batched. Shared with the Live Cast viewer (`app/routes/live_public.py`)."""
    return _rows_out(session, principal, rows, board=False)


def board_page(
    session: Session, stmt: Select, number: Optional[int], *, section: Optional[Section], focus: bool,
    after: Optional[str], limit: Optional[int], order: Optional[BoardOrder] = None,
    pinned: Sequence[int] = (),
) -> tuple[list[Task], Optional[str]]:
    """One keyset page of `stmt` (a `board_filtered_select`) in `GET /api/tasks`' order: the rows, and the
    `X-Next-Cursor` value while more remain. Shared with the Live Cast viewer, so its cursor protocol is this
    route's own."""
    cursor: Optional[str] = None
    if section is not None:
        stmt = stmt.where(task_sections.task_section_sql() == section)
    # Unfinished first, then most-recently-updated, `id desc` as tiebreak (see `_TASK_LIST_COMPLETED_GROUP`).
    # A numbered term puts its own row first: the page is clamped and the additive arm matches every body
    # mentioning the digits, which could push the named row off the page. Absent otherwise.
    number_first = () if number is None else (case((_task_number_match(number), 0), else_=1),)
    # On a focused board the queued rows are a tail (operator's call): after the unfinished/finished key,
    # so they sit at the end of the unfinished half, not below finished or hidden rows. Focus-only (empty
    # tuple otherwise), so unfocused ordering and the pickers are unchanged.
    queued_last = (
        (case((Task.state == READY, 1), else_=0),) if focus else ()
    )
    # The section's own order, after the existing grouping terms: Stopped by why, In progress by how far, Draft's
    # suggestions after its plain drafts.
    section_first: tuple[Any, ...] = (
        (func.coalesce(task_sections.task_stop_kind_rank_sql(), len(task_sections.STOP_KIND_ORDER)),)
        if section == "stopped"
        else (task_sections.in_progress_rank_sql(Task),) if section == "in_progress"
        else (task_sections.draft_rank_sql(Task),) if section == "draft"
        else ()
    )
    # `?order=section`: the State board's screen order ahead of everything but the named row, so a lazy window
    # fills section by section instead of by recency. Moot under `?section=` (one section; its rank follows).
    board_first: tuple[Any, ...] = (
        (task_sections.task_board_rank_sql(),) if order == "section" and section is None else ()
    )
    # The caller's PINNED rows (`preferences.UserPreferences.task_pinned`) lead every section they fall in: after the
    # named row and the State board's section rank, ahead of the finished/unfinished split and recency, so a pinned
    # row is in the first window of its section whatever the order. Always a term (constant without pins), so a
    # cursor's shape never depends on whether this caller pins anything.
    pinned_first = case((Task.id.in_(sorted(set(pinned))), 0), else_=1)
    # Every ORDER BY term, named for the cursor; `id` last makes the order total, so keyset paging neither
    # repeats nor skips a row.
    terms: list[tuple[str, Any, bool]] = [
        *(("number_first", e, True) for e in number_first),
        *(("board_rank", e, True) for e in board_first),
        ("pinned_first", pinned_first, True),
        ("completed_group", _TASK_LIST_COMPLETED_GROUP, True),
        *(("queued_last", e, True) for e in queued_last),
        *((f"section_rank:{section}", e, True) for e in section_first),
        ("updated_at", Task.updated_at, False),
        ("id", Task.id, False),
    ]
    shape = [name for name, _, _ in terms]
    if after is not None:
        values = keyset.decode_cursor(
            after, shape, is_stamp=lambda name: name == "updated_at",
            detail="after is not a cursor this listing issued",
        )
        stmt = stmt.where(keyset.after_clause([(e, asc) for _, e, asc in terms], values))
    page_size = _clamp_task_list_limit(limit)
    stmt = (
        stmt.add_columns(*(e.label(f"_k{i}") for i, (_, e, _) in enumerate(terms)))
        .order_by(*(e.asc() if asc else e.desc() for _, e, asc in terms))
    )
    # The correlated child subplans (`?section=`, `?order=section`) inflate the estimate past `jit_above_cost`, and
    # compiling the plan then costs several times the page itself (measured: 62 ms with JIT, 9 ms without, over
    # 8k board rows) — the trade `task_tree.count_rows` makes. Transaction-local; this read writes nothing.
    session.execute(text("SET LOCAL jit = off"))
    fetched, more = keyset.page(session, stmt, page_size, scalars=False)
    if more:
        cursor = keyset.encode_cursor(shape, list(fetched[-1][1:]))
    return [r[0] for r in fetched], cursor


def _pinned_ids(user: Optional[AdminUser]) -> list[int]:
    """The caller's pinned task ids (`UserPreferences.task_pinned`), none for a caller with no user row."""
    if user is None:
        return []
    # The membership's own document: `task_pinned` is no identity key, so no identity read (`stored_preferences`).
    return preferences.UserPreferences.from_stored(user.preferences).task_pinned


def _run_started_ats(session: Session, task_ids: list[int]) -> dict[int, datetime]:
    """Task id → its newest RUNNING agent session's `started_at`, for the page's `active` rows: the batched form of
    `routes/tasks/read.py::_live_session`, so a board card ages its run without a read per row. Pinned
    to each task's own org, as that one is: `sessions.task_id` carries no composite org key. Absent = no running
    session."""
    if not task_ids:
        return {}
    rows = session.execute(
        select(AgentSession.task_id, func.max(AgentSession.started_at))
        .join(Task, and_(Task.id == AgentSession.task_id, Task.org_id == AgentSession.org_id))
        .where(AgentSession.task_id.in_(task_ids), AgentSession.status == SESSION_RUNNING)
        .group_by(AgentSession.task_id)
    ).all()
    return {task_id: started_at for task_id, started_at in rows}


def board_rows_out(session: Session, principal: Principal | None, rows: list[Task]) -> list[TaskOut]:
    """`GET /api/tasks`' top-level rows for one page, every per-row read batched. Shared with the Live Cast
    viewer."""
    return _rows_out(session, principal, rows, board=True)


def _rows_out(session: Session, principal: Principal | None, rows: list[Task], *, board: bool) -> list[TaskOut]:
    """One `GET /api/tasks` page's rows — the board's top level (`board=True`) or one parent's children — with
    every per-row read batched: one grouped query per fact for the whole page, never a load per row, since
    both branches are unbounded and would grow with the board (an unreported gate would read as
    dispatchable; a rating or a cost shown on the board and not inside its parent would be a lie).

    The board alone carries what only a top-level row shows: child counts (a decomposed parent vs a
    childless false-close, plus the done count for the progress bar), each row's section and stop
    (`_list_placements`), and which subtask is asking (so an epic reads "Asking" while its own state stays
    `active`). A child row omits them, so `_task_out`'s defaults apply."""
    ids = [t.id for t in rows]
    tags = tag_refs_for(session, ids)
    # Dependency gates ("Waiting"), passed explicitly because this branch gives `_task_out` no session.
    gates = _unmet_prerequisite_counts(ids, session)
    # The Creator → Assignee cell: creators and assignees resolved in one query and handed to `_task_out`'s
    # map (`updated_by` is excluded); an unresolved id serialises `null`, never the uuid.
    authors = actors.actor_names(
        session, [*(t.created_by_id for t in rows), *(t.assigned_to_id for t in rows)], name_acting=True
    )
    # "Delivering", narrowed to rows still parked on a gate.
    delivering = _answer_delivering_ids([t.id for t in rows if t.state == AWAITING_RESPONSE], session)
    delivery_failures = _answer_delivery_failures(list(delivering), session)
    # Whether the row itself is asking (a runner may ask without parking); a leaf says so only through this.
    asking = _asking_task_keys(session, [(t.id, t.state, t.updated_at) for t in rows])
    # Rows writable under `tasks:create` alone (skipped for a superadmin and an unnamed caller), and under the
    # general clause the fallback refines. Not assumed empty for children: `?parent_id=` lists any parent's
    # children and authorship is a property of the row.
    authored = _authored_draft_ids(ids, session, principal)
    editable = _may_edit_ids(ids, session, principal)
    # The rows shipped to this caller and not yet opened by them; one query for the page, none when nothing is owed.
    unseen = task_done_notice.unseen_ids(session, rows, principal.user_id if principal is not None else None)
    # Five-star averages; unrated rows are absent and `.get` yields the defaults.
    ratings = rating_aggregates(session, ids)
    # Subtree costs and token totals from one scope walk (a rollup, so per row would be a subtree read
    # each); two queries total. Absent means no priceable spend / nothing captured.
    costs, tokens = run_aggregates(session, ids)
    # The page's tenants (`org_refs`); an unresolved `org_id` is absent and renders as blank.
    orgs = org_refs(session, [t.org_id for t in rows])
    # Why a ready row's org holds it back: one read per org on the page (`org_limits.wait_reasons`).
    waits = _org_wait_reasons(session, rows)
    # The slot a ready row already holds: the hover that tells it from a row waiting for a slot. Held slots
    # only, one query.
    launching = runner_places.held(session, [t.id for t in rows if t.state == READY])
    # Each row's memory record (the task view's "N injected · …"), one grouped query.
    # The community edition has no project memory: no events.
    memory: dict[int, dict[str, int]] = {}
    if edition_features.enabled("project_memory"):
        from app.project_memory import events as memory_events

        memory = memory_events.memory_counts(session, ids)
    # Each row's PRs (the list row's first PR + "+N"), one select.
    prs = task_prs.prs_by_task(session, ids)
    issues = project_links.task_issue_wire(session, ids)
    # When each active row's current run started (the board card's run age), one grouped query.
    run_starts = _run_started_ats(session, [t.id for t in rows if t.state == ACTIVE])
    # Each lane run's wall time and gate count; one query for the page's projects' last full gates, none without lanes.
    lane_run_map = merge_lane_state.lane_runs(session, rows)
    board_facts: dict[int, dict[str, Any]] = {}
    if board:
        counts = _child_counts(session, ids, principal)
        # The stoppers' names ride `authors`' map without widening it (the Creator → Assignee cell reads
        # only those two). A delivering answer places a row In progress unless a newer round still asks
        # (`stop_kind_of`).
        placements = _list_placements(
            session, principal, rows,
            {**authors, **actors.actor_names(
                session, [t.stopped_by_id for t in rows if t.stopped_by_id], name_acting=True
            )},
            delivering - asking.keys(),
        )
        gated_children = _gated_child_ids(session, ids, principal)
        board_facts = {
            t.id: dict(
                child_count=counts.get(t.id, (0, 0))[0],
                done_child_count=counts.get(t.id, (0, 0))[1],
                gated_child_id=gated_children.get(t.id),
                placement=placements[t.id],
            )
            for t in rows
        }
    # `has_repo` of every never-leased row reads its project: one read for the page, not one per project.
    slot_allocation.pin_projects(session, rows)
    # Every row a `TaskListOut`: one row shape for both branches; both resolve `created_by`, neither `updated_by`.
    outs = [
        _task_out(
            t,
            tags=tags.get(t.id),
            org_labels=orgs,
            unmet_prerequisite_count=gates.get(t.id, 0),
            actor_names=authors,
            answer_delivering=t.id in delivering,
            answer_delivery_failure=delivery_failures.get(t.id, NO_DELIVERY_FAILURE),
            has_open_question=t.id in asking,
            authored_draft=t.id in authored,
            may_edit=t.id in editable,
            done_unseen=t.id in unseen,
            rating=ratings.get(t.id),
            cost=costs.get(t.id),
            tokens=tokens.get(t.id),
            org_wait_reasons=waits,
            memory_count_map=memory,
            pr_map=prs,
            issue_map=issues,
            lane_run_map=lane_run_map,
            ran_on=launching.get(t.id),
            run_started_at=run_starts.get(t.id),
            **board_facts.get(t.id, {}),
            out_model=TaskListOut,
        )
        for t in rows
    ]
    return [_listed_draft(t, out) for t, out in zip(rows, outs, strict=True)]


def _listed_draft(task: Task, out: TaskOut) -> TaskOut:
    """An unsaved draft is listed by its body, as Save will name it (`task_title.draft_title`), not "New task".
    The detail read keeps the stored title."""
    return out.model_copy(update={"title": draft_title(task)}) if task.unsaved else out


class ScopeFilters:
    """The board's scope narrowing, declared once for the three routes the board sends the same narrowing to
    (`list_tasks`, `task_tag_counts`, `get_task_counts`), so one cannot 422 while another answers.
    `Depends(ScopeFilters)`; the attributes are `board_filtered_select`'s and `_tag_count_queries`' keyword
    names, so `**vars(scope)` passes them on."""

    def __init__(
        self,
        # The project filter. Repeatable, a union of named projects; capped like `created_by_id`. An old link's
        # scope param is not declared, so it is ignored (the list answers unfiltered).
        project_id: Optional[list[int]] = Query(None, max_length=64),
        # The feature filter: a feature's id (a string, so a malformed one is this route's own 422).
        feature_id: Optional[str] = None,
        # The user's muted nodes above the scope (`_exclusion_clauses`); repeatable, capped as `project_id`.
        exclude_project_id: Optional[list[int]] = Query(None, max_length=64),
        exclude_feature_id: Optional[list[UUID]] = Query(None, max_length=64),
        # Repeatable, a union of creators (a task has one). `Query(None)`, since a bare `list[...]` default is
        # read as a body (422 on a GET). `max_length` caps the list length, bounding the `IN (…)`; 64 far exceeds
        # the ~8-avatar strip.
        created_by_id: Optional[list[UUID]] = Query(None, max_length=64),
        # The origin filter, repeatable like `created_by_id`; codes and labels: `GET /api/tasks/sources`.
        source: Optional[list[str]] = Query(None, max_length=16),
        # Repeatable, a union of assignees (ids from `GET /api/tasks/assignees`), capped like `created_by_id`.
        assigned_to_id: Optional[list[UUID]] = Query(None, max_length=64),
    ) -> None:
        self.project_id = project_id
        self.feature_id = feature_id
        self.exclude_project_id = exclude_project_id
        self.exclude_feature_id = exclude_feature_id
        self.created_by_id = created_by_id
        self.source = source
        self.assigned_to_id = assigned_to_id


@dataclass
class BoardFilters:
    """The board's row filters, declared once for the two routes that must agree on the row set
    (`list_tasks`, `get_task_counts`), so the counts cover exactly the rows the list pages through.
    `Depends(BoardFilters)`; the fields are `board_filtered_select`'s keyword names, so `**vars(filters)`
    passes them on. A dataclass, not a hand-written `__init__`: `self.state = …` would read as a
    `Task.state` write to the task-state write-site census test."""

    state: Optional[str] = None
    persona: Optional[str] = None
    q: Optional[str] = None
    include_hidden: bool = False
    include_archived: bool = False
    exclude_done: bool = False
    # The focus toggle, server-side for correctness: the list is a `?limit=` window, so a browser-side filter
    # would narrow only the page.
    focus: bool = False
    updated_after: Optional[datetime] = None
    updated_before: Optional[datetime] = None


# `list[TaskListOut]`, never `list[TaskOut]` (that would re-serialize each row as the parent and restore the
# excluded fields). A declared model is also what puts the rows on FastAPI's `dump_json` path — one Rust pass
# to bytes, not `jsonable_encoder` walking every field (~30 ms of a 50-row page).
@router.get("", response_model=list[TaskListOut])
@feature("tasks", "view", collection=True)
@on_session
def list_tasks(
    session: Session,
    response: Response,
    parent_id: Optional[int] = None,
    filters: BoardFilters = Depends(BoardFilters),
    scope: ScopeFilters = Depends(ScopeFilters),
    limit: Optional[int] = None,
    section: Optional[Section] = None,
    after: Optional[str] = None,
    order: Optional[BoardOrder] = None,
    caller: AsyncCaller = Depends(current_caller),
) -> list[TaskOut]:
    """List Tasks. `parent_id=<id>` → that parent's children, ordered by id, unfiltered and unbounded; the
    filters and limit below do NOT apply to that branch.

    `parent_id` omitted → top-level Tasks with optional `state`/`project_id`/`persona` filters, a
    repeatable `created_by_id` / `assigned_to_id` unions (assignee ids from `GET /api/tasks/assignees`), an inclusive
    `updated_after`/`updated_before` window (`_updated_at_window`), and `q` over title, description and outcome widened by a
    partial task-number match whose rows sort first (`_task_number_term`); a pasted Jaah task link matches that
    task alone (`_task_link_term`). Ordered unfinished-first then
    most-recently-updated (`_TASK_LIST_COMPLETED_GROUP`), clamped to `TASK_LIST_MAX_LIMIT`.

    Default exclusions, each opt-in-able: `hidden=true` rows (`?include_hidden=true`), archived rows
    (`?include_archived=true`), and parentless child-only personas (unless `?persona=` names one).
    Opt-in exclusions: `?exclude_done=true` (the task `q` names by number or link stays), and `?focus=true`, which keeps `_FOCUS_TOP_LEVEL_STATES`,
    overrules `include_archived`, and sorts its `ready` rows to the tail of the unfinished half.

    Keyset paging (top-level branch): `?after=<cursor>` continues from a previous page, and the response
    carries `X-Next-Cursor` while more rows remain (absent on the last page), so rows past
    `TASK_LIST_MAX_LIMIT` are reachable. `?section=` narrows to one board section (`section_sql`);
    `?order=section` sorts by section first (`board_rank_sql`), so a window holds what the State board draws first.

    Every row on both branches is a `TaskListOut` (`body`, `awaiting_response_body`, `metrics`,
    `outcome_no_pr_evidence`, `failure_reason`, `updated_by` omitted; `created_by` kept). Read the rest from
    `GET /api/tasks/{task_id}`."""
    _user = adopt_user(session, caller.actor)
    if parent_id is not None:
        # Children of a parent: no filters, no limit, order by id. `visible_epic_children`, not a filter inside
        # `_epic_children`: that has many non-listing call sites, and dispatch must not depend on the viewer.
        # Archived rows are post-filtered.
        rows = [
            t for t in visible_epic_children(session, caller.principal, parent_id) if t.archived_at is None
        ]
        return child_rows_out(session, caller.principal, rows)

    stmt, number = board_filtered_select(caller.principal, **vars(filters), **vars(scope))
    rows, cursor = board_page(
        session, stmt, number, section=section, focus=filters.focus, after=after, limit=limit, order=order,
        pinned=_pinned_ids(_user),
    )
    if cursor is not None:
        response.headers[_NEXT_CURSOR_HEADER] = cursor
    return board_rows_out(session, caller.principal, rows)


# Declared before `/{task_id}` so the static path wins (FastAPI matches in declaration order; otherwise
# the `{task_id}:int` route 422s on it). Every static path below carries the same guard.
@router.get("/models", response_model=list[model_catalog.ModelChoice])
@feature("tasks", "view")
async def list_task_models(
    _user: AdminUser = Depends(require_user),
) -> list[model_catalog.ModelChoice]:
    """The closed set of models a Task may pin, most capable first; `INHERIT` is not a member (the form's "inherit"
    option sends the empty value). Static — no DB read; the response
    changes only when `app/model_catalog.py`'s `MODELS` does.

    Not superadmin-gated though only a superadmin may write the field (`TaskUpdate.model`): this is the
    catalogue, not a row; the write refusal is the access control."""
    return task_models()


def task_models() -> list[model_catalog.ModelChoice]:
    """`list_task_models`' answer, for a caller in process (`routes/live_public.py`)."""
    return list(model_catalog.MODELS)


class TaskSourceOut(BaseModel):
    """One origin a Task can have: the code `?source=` takes and its readable label."""

    value: str
    label: str


@router.get("/sources", response_model=list[TaskSourceOut])
@feature("tasks", "view")
async def list_task_sources(
    _user: AdminUser = Depends(require_user),
) -> list[TaskSourceOut]:
    """Every Task origin in display order — the board's Source filter reads its labels here, so they live
    only in `app/task_source.py`. Static, like `/models`."""
    return task_sources()


def task_sources() -> list[TaskSourceOut]:
    """`list_task_sources`' answer, for a caller in process (`routes/live_public.py`)."""
    return [TaskSourceOut(value=value, label=task_source.SOURCE_LABELS[value]) for value in task_source.SOURCES]


class _TagCountQueries(NamedTuple):
    """The three statements `task_tag_counts` runs, built together and handed back UNEXECUTED.

    A named builder because this route calls `visible_task_clause` directly (sanctioned only via
    `scoped_task_select`/`can_access`), and a lost correlation is a silent allow-all. Unexecuted
    statements compile, so the invariant is assertable in the DB-free quick tier,
    not only in `tests/test_task_tag_counts.py`."""

    active_tag_ids: Select
    per_tag: Select
    untagged: Select


def _tag_count_queries(
    principal: Principal | None,
    *,
    project_id: Optional[list[int]],
    include_hidden: bool,
    include_archived: bool,
    feature_id: Optional[str] = None,
    # Defaulted: `test_authz_sql.py`'s correlation guard builds the smallest predicate set, which includes an
    # unbounded window and no creator filter.
    updated_after: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
    created_by_id: Optional[list[UUID]] = None,
    focus: bool = False,
    exclude_project_id: Optional[list[int]] = None,
    exclude_feature_id: Optional[list[UUID]] = None,
    source: Optional[list[str]] = None,
    state: Optional[str] = None,
    persona: Optional[str] = None,
    assigned_to_id: Optional[list[UUID]] = None,
    within: Sequence[Any] = (),
    active_tag_ids: Optional[Select] = None,
) -> _TagCountQueries:
    """Build the tag-count statements for one request — see `task_tag_counts` for the whole contract.

    The `list_tasks` predicate mirror is assembled once and applied to both counting statements, so
    per-tag counts and `untagged` describe the same board. `within` adds counted predicates and `active_tag_ids`
    replaces the vocabulary (the public viewer, `routes/live_public.py`, pins the org the seam would)."""
    active_tag_ids = active_tag_ids_select() if active_tag_ids is None else active_tag_ids
    counted = [
        visible_task_clause(principal),
        # The board's own row predicate, `persona` included — the one `list_tasks`/`get_task_counts` use.
        *board_row_clauses(principal, include_hidden=include_hidden, include_archived=include_archived, persona=persona),
        *within,
    ]
    # Truthiness, as in `list_tasks`: `IN ()` would zero every header.
    if project_id:
        counted.append(Task.project_id.in_(sorted(project_id)))
    if feature_id is not None:
        counted.append(_feature_filter(feature_id))
    counted.extend(_exclusion_clauses(exclude_project_id, exclude_feature_id))
    counted.extend(_creator_clauses(created_by_id))
    if assigned_to_id:
        counted.append(Task.assigned_to_id.in_(assigned_to_id))
    counted.extend(_source_clauses(source))
    if state is not None:
        counted.append(Task.state == state)
    # Focus, mirrored predicate for predicate including the archived override, or totals would count rows
    # the focused list excludes. Top-level set.
    if focus:
        counted.append(Task.state.in_(sorted(_FOCUS_TOP_LEVEL_STATES)))
        counted.append(Task.archived_at.is_(None))
    # The same `_updated_at_window` helper `list_tasks` uses.
    counted.extend(_updated_at_window(updated_after, updated_before))

    # `.select_from(Task)` on both: `visible_task_clause` correlates to the enclosing `Task` FROM and
    # otherwise degrades to a cross join true for any grant holder. `per_tag` raises without it; `untagged`
    # survives only while `counted` names another `Task` column. Beware `aliased(Task)` here, which looks
    # correlated but is not.
    per_tag = (
        select(TaskTag.tag_id, func.count())
        .select_from(Task)
        .join(TaskTag, TaskTag.task_id == Task.id)
        .where(*counted, TaskTag.tag_id.in_(active_tag_ids))
        .group_by(TaskTag.tag_id)
    )
    # NOT EXISTS over active tags only, correlated to the counted row: a row tagged only in drafted groups
    # lands here.
    tagged = (
        select(literal(1))
        .select_from(TaskTag)
        .where(TaskTag.task_id == Task.id, TaskTag.tag_id.in_(active_tag_ids))
        .correlate(Task)
        .exists()
    )
    untagged = select(func.count()).select_from(Task).where(*counted, ~tagged)
    return _TagCountQueries(active_tag_ids=active_tag_ids, per_tag=per_tag, untagged=untagged)


def tag_counts_out(session: Session, queries: _TagCountQueries) -> TagCountsOut:
    """`queries` run: the zero-filled vocabulary, the per-tag counts over it, and `untagged`."""
    rows = session.execute(queries.per_tag).all()
    # Baseline first, then real counts; reversed, `update` would zero them.
    counts = {str(tag_id): 0 for tag_id in session.scalars(queries.active_tag_ids)}
    counts.update({str(tag_id): count for tag_id, count in rows})
    # No `or 0`: `select(func.count())` always returns one row.
    untagged = session.scalar(queries.untagged)
    return TagCountsOut(counts=counts, untagged=int(untagged))


class TagCountsOut(BaseModel):
    """Per-tag counts of the TOP-LEVEL Tasks the board would render, for a grouped board's section headers.

    `counts` is keyed by tag id as a string (JSON has no UUID key), one entry for EVERY tag in every
    ACTIVE group — including the tags with no rows, which carry 0. `untagged` counts the rows carrying no
    active-group tag at all."""

    counts: dict[str, int]
    untagged: int


# The board lazy-loads a growing 50-row window (`buildTasksUrl`), so client-side section counts would be
# wrong. Only the counts are server-side; no `limit` param, since a count of a window is the lie.

# Declared before `/{task_id}` (see `/models`).
@router.get("/tag-counts", response_model=TagCountsOut)
@feature("tasks", "view", collection=True)
@on_session
def task_tag_counts(
    session: Session,
    scope: ScopeFilters = Depends(ScopeFilters),
    state: Optional[str] = None,
    persona: Optional[str] = None,
    include_hidden: bool = False,
    include_archived: bool = False,
    focus: bool = False,
    updated_after: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
    caller: AsyncCaller = Depends(current_caller),
) -> TagCountsOut:
    """Per-tag counts over the TOP-LEVEL Tasks `list_tasks` would return, so a grouped board's section
    header can show a TRUE total (AC7).

    The counted set mirrors `list_tasks`' top-level branch over the params the board sends
    (`project_id`, `created_by_id`, `assigned_to_id`, `state`, `persona`, `include_hidden`, `include_archived`, `focus`, and the
    `updated_after`/`updated_before` window via the shared `_updated_at_window`); an omitted predicate
    counts rows the board never renders.

    Zero-fill is contractual: every tag of every active group appears, 0 if empty. A drafted group is
    invisible, so a Task tagged only there counts as `untagged` (`app/task_tags.py`). Multi-select tags
    double-count on purpose. `untagged` is global (no tag in any active group), by operator decision; a
    per-group map was declined."""
    _user = adopt_user(session, caller.actor)
    queries = _tag_count_queries(
        caller.principal,
        include_hidden=include_hidden,
        include_archived=include_archived,
        updated_after=updated_after,
        updated_before=updated_before,
        state=state,
        persona=persona,
        focus=focus,
        **vars(scope),
    )
    return tag_counts_out(session, queries)


class TaskAssigneeOut(BaseModel):
    """One person in a board roster — the assignee roster and the creator roster share the shape.

    `id` is the filter value (`?assigned_to_id=` / `?created_by_id=`). `name` is a label, never a filter.
    `task_count` is scoped to the request, moving with `project_id` and the opt-ins."""

    id: UUID
    name: str
    task_count: int


class RosterScope:
    """The scope both rosters (`/assignees`, `/creators`) count over, declared once so the two pickers can never
    disagree on which rows exist. Mirrors `list_tasks`' top-level branch (visibility, `parent_task_id IS NULL`,
    orphan-persona exclusion, opt-ins, `project_id`). No date-window or `focus` param, deliberately: a roster
    answers who has tasks on the board at all, and narrowing it would remove the entry needed to widen the filter
    again. `Depends(RosterScope)`."""

    def __init__(
        self,
        project_id: Optional[list[int]] = Query(None, max_length=64),
        feature_id: Optional[str] = None,
        exclude_project_id: Optional[list[int]] = Query(None, max_length=64),
        exclude_feature_id: Optional[list[UUID]] = Query(None, max_length=64),
        include_hidden: bool = False,
        include_archived: bool = False,
    ) -> None:
        self.project_id = project_id
        self.feature_id = feature_id
        self.exclude_project_id = exclude_project_id
        self.exclude_feature_id = exclude_feature_id
        self.include_hidden = include_hidden
        self.include_archived = include_archived

    def subquery(self, principal: Principal | None):
        # The board's own row predicate; no `persona=` opt-in: a roster filters the default board.
        scoped = scoped_task_select(principal).where(*board_row_clauses(
            principal, include_hidden=self.include_hidden, include_archived=self.include_archived, persona=None
        ))
        if self.project_id:
            scoped = scoped.where(Task.project_id.in_(sorted(self.project_id)))
        if self.feature_id is not None:
            scoped = scoped.where(_feature_filter(self.feature_id))
        scoped = scoped.where(*_exclusion_clauses(self.exclude_project_id, self.exclude_feature_id))
        return scoped.subquery()


def _with_caller(roster: list[TaskAssigneeOut], user: AdminUser) -> list[TaskAssigneeOut]:
    """The caller's own entry, appended only if missing (so at `task_count=0`, the end is their sorted position).
    The label comes from the principal, the same derivation, because `/api/auth/me` carries no label
    (`routes/auth.py::AdminOut`)."""
    if all(entry.id != user.id for entry in roster):
        roster.append(TaskAssigneeOut(id=user.id, name=display_name(user), task_count=0))
    return roster


# Declared before `/{task_id}` (see `/models`).
@router.get("/assignees", response_model=list[TaskAssigneeOut])
@feature("tasks", "view", collection=True)
@on_session
def task_assignees(
    session: Session,
    scope: RosterScope = Depends(RosterScope),
    caller: AsyncCaller = Depends(current_caller),
) -> list[TaskAssigneeOut]:
    """The DISTINCT assignees of the top-level Tasks in scope (`RosterScope`), with each one's row count — the
    roster the board's assignee filter draws.

    Ordered by `task_count` desc then label asc, not recency: recency would reshuffle the strip under the
    pointer. The caller is always present, at their count or 0 (`_with_caller`)."""
    user = adopt_user(session, caller.actor)
    in_scope = scope.subquery(caller.principal)

    # One grouped query. Grouped by id, labelled after: two people may share a label
    # (`identities.label_expr`), and each is their own avatar.
    label = label_expr().label("label")
    rows = session.execute(
        select(AdminUser.id, label, func.count())
        .select_from(in_scope)
        # Inner join: unassigned rows (and deleted actors) contribute nothing; an "unassigned" avatar would
        # have no id to filter by.
        .join(AdminUser, AdminUser.id == in_scope.c.assigned_to_id)
        .group_by(AdminUser.id)
        .order_by(func.count().desc(), label.asc())
    ).all()
    return _with_caller(
        [TaskAssigneeOut(id=assignee_id, name=name, task_count=task_count) for assignee_id, name, task_count in rows],
        user,
    )


# Declared before `/{task_id}` (see `/models`).
@router.get("/creators", response_model=list[TaskAssigneeOut])
@feature("tasks", "view", collection=True)
@on_session
def task_creators(
    session: Session,
    scope: RosterScope = Depends(RosterScope),
    caller: AsyncCaller = Depends(current_caller),
) -> list[TaskAssigneeOut]:
    """The DISTINCT creators of the top-level Tasks in scope (`RosterScope`), with each one's row count — the
    roster the board's Creator filter draws. Same order and caller rule as `task_assignees`.

    Rows no human filed — stamped `SYSTEM_ACTOR_ID`, or with no recorded creator (NULL) — fold into ONE
    "System" entry under `SYSTEM_ACTOR_ID`, which `_creator_clauses` reads back as both. Names resolve through
    `actors.actor_names`, the provenance cells' own resolver, so the roster says what the Creator column says. An
    id that resolves to nothing is dropped, and so is one a tenant reader sees only as `WITHHELD_ACTOR` (staff): as
    the assignee roster's tenant-scoped join drops them, so no staff id reaches a tenant."""
    user = adopt_user(session, caller.actor)
    in_scope = scope.subquery(caller.principal)
    creator = func.coalesce(in_scope.c.created_by_id, literal(SYSTEM_ACTOR_ID, type_=Task.created_by_id.type))
    rows = session.execute(select(creator, func.count()).select_from(in_scope).group_by(creator)).all()
    names = actors.actor_names(session, [creator_id for creator_id, _ in rows], name_acting=True)
    roster = [
        TaskAssigneeOut(id=creator_id, name=names[creator_id], task_count=task_count)
        for creator_id, task_count in rows
        if creator_id in names and (creator_id == SYSTEM_ACTOR_ID or names[creator_id] != WITHHELD_ACTOR)
    ]
    roster.sort(key=lambda entry: (-entry.task_count, entry.name.casefold()))
    return _with_caller(roster, user)


# ── The task rail: tree + counts (`app/task_tree.py`) ─────────────────────────────────────────────────
# Declared before `/{task_id}` (see `/models`).


@router.get("/tree", response_model=task_tree.TaskTree)
@feature("tasks", "view", collection=True)
@on_session
def get_task_tree(
    session: Session,
    caller: AsyncCaller = Depends(current_caller),
) -> task_tree.TaskTree:
    """The whole task rail for the caller in one call: every project they may see tasks in, each
    project's live features (and the deleted ones still holding a row, by name), every node with its `Counts` over the
    default board (`board_row_clauses`), parents summed from their children."""
    _user = adopt_user(session, caller.actor)
    board = scoped_task_select(caller.principal).where(
        *board_row_clauses(caller.principal, include_hidden=False, include_archived=False, persona=None)
    )
    return task_tree.build_tree(
        session, caller.principal, board, scope=scoped_task_select(caller.principal), board_key="rail-default-board"
    )


@router.get("/counts", response_model=task_tree.Counts)
@feature("tasks", "view", collection=True)
@on_session
def get_task_counts(
    session: Session,
    filters: BoardFilters = Depends(BoardFilters),
    scope: ScopeFilters = Depends(ScopeFilters),
    caller: AsyncCaller = Depends(current_caller),
) -> task_tree.Counts:
    """`Counts` for exactly the rows `GET /api/tasks` pages through under the same filters
    (`board_filtered_select`) — every top-level filter but `limit`/`section`/`after`, which narrow or page
    the list rather than choose its set. `parent_id` is not offered: it lists children, another unit. With
    no filters it equals `GET /api/tasks/tree`'s root `counts`."""
    _user = adopt_user(session, caller.actor)
    board, _number = board_filtered_select(caller.principal, **vars(filters), **vars(scope))
    return task_tree.counts_of(session, board)


# ── The persona registry, served ─────────────────────────────────────────────────────────────────────
# Declared before `/{task_id}` (see `/models`).


class PersonaRegistryRowOut(BaseModel):
    """One `app/persona_registry.py::Persona` record on the wire, field for field.

    Owns no fact: every value is read off the record at request time. `Optional` where the record's field
    is (no charter = not dispatchable; no `option_label` = not offered)."""

    name: str
    label: str
    option_label: Optional[str]
    hint: Optional[str]
    operator_pickable: bool
    pipeline: bool
    dispatchable: bool
    charter_key: Optional[str]
    default_model: Optional[str]
    mode: Optional[str]
    delivery: Optional[str]
    done_criterion: Optional[str]
    terminal_types: list[str]
    settle_states: list[str]
    creation_shapes: list[str]
    # May a project's `default_persona` name it (`PROJECT_DEFAULT_PERSONAS`) — the project page's picker filters on it.
    project_default: bool


class PersonaRegistryOut(BaseModel):
    personas: list[PersonaRegistryRowOut]
    picker_hint: str


@router.get("/personas", response_model=PersonaRegistryOut)
@feature("tasks", "view")
async def task_personas(
    _user: AdminUser = Depends(require_user),
) -> PersonaRegistryOut:
    """The whole persona vocabulary, in registry order — what `apps/web` renders the Persona picker from.

    The web keeps no copy: options, order, hints and field copy arrive from here, so a registered persona
    reaches the picker automatically. System personas are included: the Needs-attention view renders
    hidden rows and needs their labels; the picker filters on `operator_pickable`.

    `tasks:view` (every task reader renders the picker), unlike `GET /api/personas`
    (`app/routes/personas.py`, `tasks:create`), the Audit > Personas roster with role prose. No `session`: it reads no
    row, only compiled registry records."""
    return persona_registry()


def persona_registry() -> PersonaRegistryOut:
    """`task_personas`' answer, for a caller in process (`routes/live_public.py`)."""
    return PersonaRegistryOut(
        personas=[
            from_row(
                PersonaRegistryRowOut, p,
                default_model=p.model,
                terminal_types=list(p.terminals),
                settle_states=list(p.settle_states),
                creation_shapes=sorted(TASK_CREATION_LICENCE.shapes),
                project_default=p.name in PROJECT_DEFAULT_PERSONAS,
            )
            for p in PERSONA_RECORDS
        ],
        picker_hint=PERSONA_PICKER_HINT,
    )
