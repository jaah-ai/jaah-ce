"""Task listing: search, the board, list, facets, tree, counts and the persona registry."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NamedTuple, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import Select, String, and_, case, cast, func, literal, or_, select, text
from sqlalchemy.orm import Session

from app import actors, edition_features, model_catalog
from app.clock import as_utc
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import (
    scoped_task_select,
    visible_epic_children,
    unhidden_task_clause,
    visible_task_clause,
)
from app.db import get_session
from app.entity_shape import optional_uuid
from app.list_page import like_pattern
from app.agent_sessions import STATUS_RUNNING as SESSION_RUNNING
from app.models.agent_sessions import AgentSession
from app.models.identity import AdminUser
from app.models.task_extras import TaskTag
from app.models.tasks import Task
from app.identities import display_name, label_expr
from app.persona_registry import PERSONA_PICKER_HINT, PERSONA_RECORDS, PROJECT_DEFAULT_PERSONAS
from app import runner_places, task_prs
from app import keyset, task_sections
from app import task_tree
from app.task_sections import Section
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, READY, _FOCUS_TOP_LEVEL_STATES
from app import task_source
from app.task_run_metrics import run_aggregates
from app.task_ratings import rating_aggregates
from app.task_tags import active_tag_ids_select, tag_refs_for
from app.task_title import draft_title
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
    _ran_on_of,
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


# ── Free-text search over title/body ────────────────────────────────────────────────────────────────
# LIKE escape char (`list_page.like_pattern` escapes with it); a term is a LITERAL substring (match
# correctness, not injection — it is bound anyway).
_LIKE_ESCAPE_CHAR = "\\"


# ── The NUMBER a search term names, when it names one ───────────────────────────────────────────────
# Single home for `?q=`'s number arm: `42`, `#42`, `task #42`, whitespace optional; leading zeros
# collapse. `[0-9]` not `\d` (non-ASCII digits). Nine digits because `tasks.id` is int4. `(?:#\s*)?`
# not `#?\s*`: two competing `\s*` are O(n²) on a whitespace run, on the single uvicorn worker.
_TASK_NUMBER_TERM = re.compile(r"^(?:task\s*)?(?:#\s*)?([0-9]{1,9})$", re.IGNORECASE)

# `task #` plus nine digits is 15 chars; generous room for whitespace. The structural half of the O(n²) fix.
_MAX_NUMBER_TERM_LEN = 32


def _task_number_term(term: str) -> Optional[int]:
    """The task NUMBER a `?q=` term names, or `None` (ordinary free text). Purely lexical. A number
    WIDENS the title/body match by a task-id match, never replaces it."""
    stripped = term.strip()
    # Length refused BEFORE the pattern runs, so no term's cost depends on input length.
    if len(stripped) > _MAX_NUMBER_TERM_LEN:
        return None
    match = _TASK_NUMBER_TERM.match(stripped)
    return int(match.group(1)) if match else None


def _task_number_match(number: int) -> Any:
    """Predicate: this row's id CONTAINS the digits of `number` — PARTIAL (`196` finds `1964`), via TEXT
    cast + LIKE. No escaping needed: `number` is ASCII digits only. One home because `list_tasks` uses it
    in both `WHERE` and `ORDER BY`, and the two must describe the same row."""
    pattern = f"%{number}%"
    return cast(Task.id, String).like(pattern)


# ── `updated_at` window shared by the list route and its tag-count mirror ───────────────────────────
# One home, so the list and `task_tag_counts` apply the same edge rule. Both bounds INCLUSIVE, independent.
def _as_utc_bound(value: datetime) -> datetime:
    """A caller-supplied `updated_at` bound as an AWARE datetime; a NAIVE one is read as UTC rather than
    422ed. Left naive, Postgres would cast it with the SESSION `TimeZone`, shifting the window on a server
    setting no caller can see."""
    return as_utc(value)


def _updated_at_window(
    updated_after: Optional[datetime], updated_before: Optional[datetime]
) -> list[Any]:
    """The `updated_at >= after` / `<= before` predicates for the bounds supplied; `[]` for neither, so
    callers splat it unconditionally."""
    window: list[Any] = []
    if updated_after is not None:
        window.append(Task.updated_at >= _as_utc_bound(updated_after))
    if updated_before is not None:
        window.append(Task.updated_at <= _as_utc_bound(updated_before))
    return window


# `list_tasks`'s one sort key: 0 = still actionable, 1 = finished business. An archived row is never
# unfinished; a hidden row is finished business even while running (hidden rows outnumber visible ~4:1).
# A hidden Runner-cron row the operator chose to show also sorts as finished — accepted: `hidden AND not
# live` would lift every running system row, and reading `persona` here is forbidden (P3b;
# `models/tasks.py:Task.hidden`). It orders the route, so the web pickers reading this bounded list lose
# finished rows past `?limit=`. Client half: `isCompletedRow` (web, tasks/shared).
_TASK_LIST_COMPLETED_GROUP = case(
    (Task.state == DONE, 1),
    (Task.archived_at.isnot(None), 1),
    # A hidden row asking a question is unfinished work somebody owes an answer (`unhidden_task_clause`).
    (and_(Task.hidden.is_(True), Task.state != AWAITING_RESPONSE), 1),
    else_=0,
)


# The `?feature_id=` literal for "No feature".
_NO_FEATURE = "none"


def _feature_filter(raw: str):
    """The `GET /api/tasks?feature_id=` predicate: `none` → unfiled rows, a uuid → that feature's, else 422.
    The access clause is untouched: a feature the caller cannot see simply matches no row they can."""
    if raw == _NO_FEATURE:
        return Task.feature_id.is_(None)
    feature_id = optional_uuid(raw)
    if feature_id is None:
        raise HTTPException(
            status_code=422, detail=f"feature_id must be a feature's id or '{_NO_FEATURE}'"
        )
    return Task.feature_id == feature_id


def _exclusion_clauses(
    exclude_project_id: Optional[list[int]], exclude_feature_id: Optional[list[UUID]]
) -> list[Any]:
    """The board's `exclude_project_id=` / `exclude_feature_id=` predicates: drop the named projects' rows and
    the named features' rows (a no-feature row is never dropped by the second). The web sends a user's MUTED
    nodes above the selected scope (`lib/taskMute.ts`); the server holds no mute rule of its own. Truthiness, as
    `project_id`: an empty list would compile to `NOT IN ()`."""
    clauses: list[Any] = []
    if exclude_project_id:
        clauses.append(Task.project_id.not_in(sorted(exclude_project_id)))
    if exclude_feature_id:
        clauses.append(or_(Task.feature_id.is_(None), Task.feature_id.not_in(sorted(exclude_feature_id))))
    return clauses


def board_row_clauses(*, include_hidden: bool, include_archived: bool, persona: Optional[str]) -> list[Any]:
    """THE board's top-level-row predicate — which rows the task board is made of before any narrowing
    filter: top-level only, `hidden` rows unless opted in, archived rows unless opted in, and the
    parentless child-only personas unless `persona` names one (then only that persona). One home, reused
    by every route that must describe the same board."""
    clauses: list[Any] = [Task.parent_task_id.is_(None)]
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
) -> tuple[Select, Optional[int]]:
    """`GET /api/tasks`' top-level row set: the caller's visible board rows narrowed by every list filter
    but `section`, `limit` and `after`, plus the task number `q` names (it orders the page). One home, so
    `GET /api/tasks/counts` counts exactly the rows the list pages through."""
    stmt = scoped_task_select(principal).where(
        *board_row_clauses(include_hidden=include_hidden, include_archived=include_archived, persona=persona)
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
    if exclude_done:
        # For the prerequisite picker (`components/tasks/TaskDependencies.tsx`); server-side, since a
        # client-side filter over a clamped page would drop candidates past `?limit=`.
        stmt = stmt.where(Task.state != DONE)
    # Truthiness, as for `created_by_id`: an empty list would compile to `IN ()` and empty the board.
    if project_id:
        stmt = stmt.where(Task.project_id.in_(sorted(project_id)))
    if feature_id is not None:
        stmt = stmt.where(_feature_filter(feature_id))
    stmt = stmt.where(*_exclusion_clauses(exclude_project_id, exclude_feature_id))
    # The creator filter, by id never by the `created_by` label (names can collide or change). Truthiness,
    # not `is not None`: an empty list would compile to `IN ()` and empty the board.
    if created_by_id:
        stmt = stmt.where(Task.created_by_id.in_(created_by_id))
    stmt = stmt.where(*_source_clauses(source))
    # Free-text search: case-insensitive literal substring on title or body, widened by the row's number
    # when the term names one (`_task_number_term`). A blank `q` is ignored; bound via `ilike` with `\` as
    # the escape char. The number arm is additive and partial (`196` matches #1964 and any text with those
    # digits), so no previous result is lost.
    number = _task_number_term(q) if q is not None else None
    if q is not None and q.strip():
        pattern = like_pattern(q)
        text_match = (
            Task.title.ilike(pattern, escape=_LIKE_ESCAPE_CHAR)
            | Task.body.ilike(pattern, escape=_LIKE_ESCAPE_CHAR)
        )
        stmt = stmt.where(text_match if number is None else text_match | _task_number_match(number))
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
) -> tuple[list[Task], Optional[str]]:
    """One keyset page of `stmt` (a `board_filtered_select`) in `GET /api/tasks`' order: the rows, and the
    `X-Next-Cursor` value while more remain. Shared with the Live Cast viewer, so its cursor protocol is this
    route's own."""
    cursor: Optional[str] = None
    if section is not None:
        stmt = stmt.where(task_sections.section_sql(Task) == section)
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
    # The section's own order, after the existing grouping terms: Stopped by why, In progress by how far.
    section_first: tuple[Any, ...] = (
        (func.coalesce(task_sections.stop_kind_rank_sql(Task), len(task_sections.STOP_KIND_ORDER)),)
        if section == "stopped"
        else (task_sections.in_progress_rank_sql(Task),) if section == "in_progress"
        else ()
    )
    # `?order=section`: the State board's screen order ahead of everything but the named row, so a lazy window
    # fills section by section instead of by recency. Moot under `?section=` (one section; its rank follows).
    board_first: tuple[Any, ...] = (
        (task_sections.board_rank_sql(Task),) if order == "section" and section is None else ()
    )
    # Every ORDER BY term, named for the cursor; `id` last makes the order total, so keyset paging neither
    # repeats nor skips a row.
    terms: list[tuple[str, Any, bool]] = [
        *(("number_first", e, True) for e in number_first),
        *(("board_rank", e, True) for e in board_first),
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
        .limit(page_size + 1)
    )
    # The correlated child subplans (`?section=`, `?order=section`) inflate the estimate past `jit_above_cost`, and
    # compiling the plan then costs several times the page itself (measured: 62 ms with JIT, 9 ms without, over
    # 8k board rows) — the trade `task_tree.count_rows` makes. Transaction-local; this read writes nothing.
    session.execute(text("SET LOCAL jit = off"))
    fetched = session.execute(stmt).all()
    if len(fetched) > page_size:
        cursor = keyset.encode_cursor(shape, list(fetched[page_size - 1][1:]))
    rows = [r[0] for r in fetched[:page_size]]
    return rows, cursor


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
    # The Author column: creators resolved in one query and handed to `_task_out`'s map; creator only
    # (`updated_by` is excluded); an unresolved id serialises `null`, never the uuid.
    authors = actors.actor_names(session, [t.created_by_id for t in rows], name_acting=True)
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
    # When each active row's current run started (the board card's run age), one grouped query.
    run_starts = _run_started_ats(session, [t.id for t in rows if t.state == ACTIVE])
    board_facts: dict[int, dict[str, Any]] = {}
    if board:
        counts = _child_counts(session, ids, principal)
        # The stoppers' names ride `authors`' map without widening it (the Author column reads only
        # creators). A delivering answer places a row In progress unless a newer round still asks
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
            rating=ratings.get(t.id),
            cost=costs.get(t.id),
            tokens=tokens.get(t.id),
            org_wait_reasons=waits,
            memory_count_map=memory,
            pr_map=prs,
            ran_on=_ran_on_of(launching.get(t.id)),
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
        # The feature filter: a feature's id, or `none` for the unfiled rows ("No feature" — of the named
        # project(s) when `project_id` is given). A string, not a `UUID`, for the `none` literal;
        # anything else is a 422.
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
    ) -> None:
        self.project_id = project_id
        self.feature_id = feature_id
        self.exclude_project_id = exclude_project_id
        self.exclude_feature_id = exclude_feature_id
        self.created_by_id = created_by_id
        self.source = source


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
def list_tasks(
    # Injected: carries `X-Next-Cursor`.
    response: Response,
    parent_id: Optional[int] = None,
    filters: BoardFilters = Depends(BoardFilters),
    scope: ScopeFilters = Depends(ScopeFilters),
    limit: Optional[int] = None,
    # The board section (`app/task_sections.py`): a parent is placed by its children. Stopped sorts by
    # `STOP_KIND_ORDER` and In progress by `IN_PROGRESS_ORDER` ahead of the recency terms.
    section: Optional[Section] = None,
    # Keyset paging: the `X-Next-Cursor` a previous page returned. Valid only for the same ordering.
    after: Optional[str] = None,
    # `section`: the State board's screen order (`task_sections.board_rank_sql`) ahead of the recency terms.
    order: Optional[BoardOrder] = None,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[TaskOut]:
    """List Tasks. `parent_id=<id>` → that parent's children, ordered by id, unfiltered and unbounded; the
    filters and limit below do NOT apply to that branch.

    `parent_id` omitted → top-level Tasks with optional `state`/`project_id`/`persona` filters, a
    repeatable `created_by_id` union (ids from `GET /api/tasks/creators`), an inclusive
    `updated_after`/`updated_before` window (`_updated_at_window`), and `q` over title/body widened by a
    partial task-number match whose rows sort first (`_task_number_term`). Ordered unfinished-first then
    most-recently-updated (`_TASK_LIST_COMPLETED_GROUP`), clamped to `TASK_LIST_MAX_LIMIT`.

    Default exclusions, each opt-in-able: `hidden=true` rows (`?include_hidden=true`), archived rows
    (`?include_archived=true`), and parentless child-only personas (unless `?persona=` names one).
    Opt-in exclusions: `?exclude_done=true`, and `?focus=true`, which keeps `_FOCUS_TOP_LEVEL_STATES`,
    overrules `include_archived`, and sorts its `ready` rows to the tail of the unfinished half.

    Keyset paging (top-level branch): `?after=<cursor>` continues from a previous page, and the response
    carries `X-Next-Cursor` while more rows remain (absent on the last page), so rows past
    `TASK_LIST_MAX_LIMIT` are reachable. `?section=` narrows to one board section (`section_sql`);
    `?order=section` sorts by section first (`board_rank_sql`), so a window holds what the State board draws first.

    Every row on both branches is a `TaskListOut` (`body`, `awaiting_response_body`, `metrics`,
    `outcome_no_pr_evidence`, `failure_reason`, `updated_by` omitted; `created_by` kept). Read the rest from
    `GET /api/tasks/{task_id}`."""
    if parent_id is not None:
        # Children of a parent: no filters, no limit, order by id. `visible_epic_children`, not a filter inside
        # `_epic_children`: that has many non-listing call sites, and dispatch must not depend on the viewer.
        # Archived rows are post-filtered.
        rows = [
            t for t in visible_epic_children(session, principal, parent_id) if t.archived_at is None
        ]
        return child_rows_out(session, principal, rows)

    stmt, number = board_filtered_select(principal, **vars(filters), **vars(scope))
    rows, cursor = board_page(
        session, stmt, number, section=section, focus=filters.focus, after=after, limit=limit, order=order,
    )
    if cursor is not None:
        response.headers[_NEXT_CURSOR_HEADER] = cursor
    return board_rows_out(session, principal, rows)


class TaskModelOut(BaseModel):
    """One selectable model on the wire — `model_catalog.ModelChoice`, field for field.

    The console's select renders from this route only: the closed set, order and wording live once in
    `app/model_catalog.py`. `INHERIT` is not a member; the form's "inherit" option sends the empty value."""

    value: str
    label: str
    description: str


# Declared before `/{task_id}` so the static path wins (FastAPI matches in declaration order; otherwise
# the `{task_id}:int` route 422s on it). Every static path below carries the same guard.
@router.get("/models", response_model=list[TaskModelOut])
@feature("tasks", "view")
def list_task_models(
    _user: AdminUser = Depends(require_user),
) -> list[TaskModelOut]:
    """The closed set of models a Task may pin, most capable first. Static — no DB read; the response
    changes only when `app/model_catalog.py`'s `MODELS` does.

    Not superadmin-gated though only a superadmin may write the field (`TaskUpdate.model`): this is the
    catalogue, not a row; the write refusal is the access control."""
    return [
        TaskModelOut(value=c.value, label=c.label, description=c.description)
        for c in model_catalog.MODELS
    ]


class TaskSourceOut(BaseModel):
    """One origin a Task can have: the code `?source=` takes and its readable label."""

    value: str
    label: str


@router.get("/sources", response_model=list[TaskSourceOut])
@feature("tasks", "view")
def list_task_sources(
    _user: AdminUser = Depends(require_user),
) -> list[TaskSourceOut]:
    """Every Task origin in display order — the board's Source filter reads its labels here, so they live
    only in `app/task_source.py`. Static, like `/models`."""
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
) -> _TagCountQueries:
    """Build the tag-count statements for one request — see `task_tag_counts` for the whole contract.

    The `list_tasks` predicate mirror is assembled once and applied to both counting statements, so
    per-tag counts and `untagged` describe the same board."""
    active_tag_ids = active_tag_ids_select()
    counted = [
        Task.parent_task_id.is_(None),
        visible_task_clause(principal),
        # No NULL arm needed — the column is NOT NULL, so a plain NOT IN cannot drop a row to UNKNOWN.
        ~Task.persona.in_(_ORPHAN_CHILD_PERSONAS),
    ]
    if not include_hidden:
        counted.append(unhidden_task_clause())
    if not include_archived:
        counted.append(Task.archived_at.is_(None))
    # Truthiness, as in `list_tasks`: `IN ()` would zero every header.
    if project_id:
        counted.append(Task.project_id.in_(sorted(project_id)))
    if feature_id is not None:
        counted.append(_feature_filter(feature_id))
    counted.extend(_exclusion_clauses(exclude_project_id, exclude_feature_id))
    # Truthiness, as in `list_tasks`: `IN ()` would zero every header.
    if created_by_id:
        counted.append(Task.created_by_id.in_(created_by_id))
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
def task_tag_counts(
    # The scope filter; mirrors `list_tasks`' params because the board sends both the same narrowing (a
    # feature or "No feature" scope adds `feature_id`).
    scope: ScopeFilters = Depends(ScopeFilters),
    # The board's State filter, as `list_tasks`' own param.
    state: Optional[str] = None,
    include_hidden: bool = False,
    include_archived: bool = False,
    # Mirrors `list_tasks`' own param.
    focus: bool = False,
    updated_after: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TagCountsOut:
    """Per-tag counts over the TOP-LEVEL Tasks `list_tasks` would return, so a grouped board's section
    header can show a TRUE total (AC7).

    The counted set mirrors `list_tasks`' top-level branch over the params the board sends
    (`project_id`, `created_by_id`, `state`, `include_hidden`, `include_archived`, `focus`, and the
    `updated_after`/`updated_before` window via the shared `_updated_at_window`); an omitted predicate
    counts rows the board never renders.

    Zero-fill is contractual: every tag of every active group appears, 0 if empty. A drafted group is
    invisible, so a Task tagged only there counts as `untagged` (`app/task_tags.py`). Multi-select tags
    double-count on purpose. `untagged` is global (no tag in any active group), by operator decision; a
    per-group map was declined."""
    queries = _tag_count_queries(
        principal,
        include_hidden=include_hidden,
        include_archived=include_archived,
        updated_after=updated_after,
        updated_before=updated_before,
        state=state,
        focus=focus,
        **vars(scope),
    )
    rows = session.execute(queries.per_tag).all()
    # Baseline first, then real counts; reversed, `update` would zero them.
    counts = {str(tag_id): 0 for tag_id in session.scalars(queries.active_tag_ids)}
    counts.update({str(tag_id): count for tag_id, count in rows})
    # No `or 0`: `select(func.count())` always returns one row.
    untagged = session.scalar(queries.untagged)
    return TagCountsOut(counts=counts, untagged=int(untagged))


class TaskCreatorOut(BaseModel):
    """One entry of the board's creator roster: who to filter by, what to draw, and how big they are.

    `id` is the filter value for `?created_by_id=`, available only here (list rows carry the creator's
    name, never an actor id; pinned in `tests/test_tasks_routes.py`). `name` is a label, never a filter.
    `task_count` is scoped to the request, moving with `project_id` and the opt-ins."""

    id: UUID
    name: str
    task_count: int


# Declared before `/{task_id}` (see `/models`).
@router.get("/creators", response_model=list[TaskCreatorOut])
@feature("tasks", "view", collection=True)
def task_creators(
    # THE SCOPE FILTER — see `list_tasks`.
    project_id: Optional[list[int]] = Query(None, max_length=64),
    feature_id: Optional[str] = None,
    # The user's muted nodes above the scope (`_exclusion_clauses`); repeatable, capped as `project_id`.
    exclude_project_id: Optional[list[int]] = Query(None, max_length=64),
    exclude_feature_id: Optional[list[UUID]] = Query(None, max_length=64),
    include_hidden: bool = False,
    include_archived: bool = False,
    # `user`, not `_user`: the answer depends on who asks (see the caller's-own-entry rule).
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[TaskCreatorOut]:
    """The DISTINCT creators of the top-level Tasks in scope, with each one's row count — the roster
    the board's creator-filter strip draws.

    The scope mirrors `list_tasks`' top-level branch (visibility, `parent_task_id IS NULL`,
    orphan-persona exclusion, opt-ins, `project_id`). No date-window or `focus` param, deliberately:
    this answers who has tasks on the board at all, and narrowing it would remove the avatar needed to
    widen the filter again.

    Ordered by `task_count` desc then label asc, not recency: the strip shows ~8 avatars, and recency
    would reshuffle them under the pointer. The caller is always present, at their count or 0, settled
    server-side because `/api/auth/me` carries no label (`routes/auth.py::AdminOut`)."""
    # Built from `scoped_task_select` and aggregated as a subquery — the sanctioned shape, since the
    # aggregate is over tasks (unlike `_tag_count_queries`, which needs a DB-free correlation guard).
    # Postgres flattens it.
    scoped = scoped_task_select(principal).where(
        Task.parent_task_id.is_(None),
        # No NULL arm (NOT NULL column). No `persona=` opt-in: the strip filters the default board.
        ~Task.persona.in_(_ORPHAN_CHILD_PERSONAS),
    )
    if not include_hidden:
        scoped = scoped.where(unhidden_task_clause())
    if not include_archived:
        scoped = scoped.where(Task.archived_at.is_(None))
    if project_id:
        scoped = scoped.where(Task.project_id.in_(sorted(project_id)))
    if feature_id is not None:
        scoped = scoped.where(_feature_filter(feature_id))
    scoped = scoped.where(*_exclusion_clauses(exclude_project_id, exclude_feature_id))
    in_scope = scoped.subquery()

    # One grouped query, instead of ids plus a label lookup plus counts. Grouped by id, labelled after: two
    # people may share a label (`identities.label_expr`), and each is their own avatar.
    label = label_expr().label("label")
    rows = session.execute(
        select(AdminUser.id, label, func.count())
        .select_from(in_scope)
        # Inner join: rows with no `created_by_id` (machine-written top-level rows, deleted actors) contribute
        # nothing; an "unattributed" avatar would have no id to filter by.
        .join(AdminUser, AdminUser.id == in_scope.c.created_by_id)
        .group_by(AdminUser.id)
        .order_by(func.count().desc(), label.asc())
    ).all()
    creators = [
        TaskCreatorOut(id=creator_id, name=name, task_count=task_count)
        for creator_id, name, task_count in rows
    ]
    # The caller's own entry, appended only if missing; they can only be missing with `task_count=0`, so the
    # end is their sorted position. Placement beyond that is the web's business. The label comes from the
    # principal, the same derivation.
    if all(creator.id != user.id for creator in creators):
        creators.append(TaskCreatorOut(id=user.id, name=display_name(user), task_count=0))
    return creators


# ── The task rail: tree + counts (`app/task_tree.py`) ─────────────────────────────────────────────────
# Declared before `/{task_id}` (see `/models`).


@router.get("/tree", response_model=task_tree.TaskTree)
@feature("tasks", "view", collection=True)
def get_task_tree(
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> task_tree.TaskTree:
    """The whole task rail for the caller in one call: every project they may see tasks in, each
    project's features (shipped included) and unfiled rows, every node with its `Counts` over the
    default board (`board_row_clauses`), parents summed from their children."""
    board = scoped_task_select(principal).where(
        *board_row_clauses(include_hidden=False, include_archived=False, persona=None)
    )
    return task_tree.build_tree(session, principal, board)


@router.get("/counts", response_model=task_tree.Counts)
@feature("tasks", "view", collection=True)
def get_task_counts(
    filters: BoardFilters = Depends(BoardFilters),
    scope: ScopeFilters = Depends(ScopeFilters),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> task_tree.Counts:
    """`Counts` for exactly the rows `GET /api/tasks` pages through under the same filters
    (`board_filtered_select`) — every top-level filter but `limit`/`section`/`after`, which narrow or page
    the list rather than choose its set. `parent_id` is not offered: it lists children, another unit. With
    no filters it equals `GET /api/tasks/tree`'s root `counts`."""
    board, _number = board_filtered_select(principal, **vars(filters), **vars(scope))
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
def task_personas(
    _user: AdminUser = Depends(require_user),
) -> PersonaRegistryOut:
    """The whole persona vocabulary, in registry order — what `apps/web` renders the Persona picker from.

    The web keeps no copy: options, order, hints and field copy arrive from here, so a registered persona
    reaches the picker automatically. System personas are included: the Needs-attention view renders
    hidden rows and needs their labels; the picker filters on `operator_pickable`.

    `tasks:view` (every task reader renders the picker), unlike `GET /api/personas`
    (`app/routes/personas.py`, `tasks:create`), the Audit > Personas roster with role prose. No `session`: it reads no
    row, only compiled registry records."""
    return PersonaRegistryOut(
        personas=[
            PersonaRegistryRowOut(
                name=p.name,
                label=p.label,
                option_label=p.option_label,
                hint=p.hint,
                operator_pickable=p.operator_pickable,
                pipeline=p.pipeline,
                dispatchable=p.dispatchable,
                charter_key=p.charter_key,
                default_model=p.model,
                mode=p.mode,
                delivery=p.delivery,
                done_criterion=p.done_criterion,
                terminal_types=list(p.terminals),
                settle_states=list(p.settle_states),
                creation_shapes=sorted(p.licence.shapes) if p.licence else [],
                project_default=p.name in PROJECT_DEFAULT_PERSONAS,
            )
            for p in PERSONA_RECORDS
        ],
        picker_hint=PERSONA_PICKER_HINT,
    )
