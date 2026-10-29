"""Worktree-slot Title — the CP's one decision table, for both the read and the write concern.

The Title is CP-owned: no node-sensed value ever becomes one; only the operator's declaration survives.
The column is `worktrees.name` (NOT NULL); the wire field is `title`, `resolved_title`'s output.
`stored_opinion` and `name_to_store` carry the "operator said nothing" distinction NULL cannot.

`resolved_title` is the read seam's fallback and `title_for_declaration` the write seam's decision
(`None` = leave it alone). Resolution lives here because there are two payload builders; it reads the
task through the `tasks` LEFT JOIN, so a slot whose title write was dropped still shows a real Title.
"""
from __future__ import annotations

import re

from app.task_lifecycle import AWAITING_RESPONSE, _ACTIVE_TASK_STATES

# Imported, not spelled, so it cannot drift from `Worktree.state`'s vocabulary. `slot_state` never
# imports this module.
from app.slot_state import FREE

# What a slot with no stored Title and no active task reads as. A word, not `""`: a blank cell looks
# like a broken read. Kept separate from `FREE` at use sites — rule 2 tests the state and decides the Title.
FREE_TITLE = FREE


def task_active_from_state(task_state: str | None) -> bool:
    """The "active task" predicate, over the `tasks.state` a slot's LEFT JOIN produced.

    `None` covers both a NULL `task_id` and one dangling at a deleted row (no FK). Membership is
    `task_lifecycle._ACTIVE_TASK_STATES`."""
    return task_state is not None and task_state in _ACTIVE_TASK_STATES


def task_awaiting_answer_from_state(task_state: str | None) -> bool:
    """Is the task holding this slot parked on a human answer — the slot's hold reason, not its liveness.

    Without it, a sensed `waiting` status plus a growing idle clock reads as a dead runner. Decided
    CP-side so no client grows its own task-state list. `None` (no task, or dangling) is False."""
    return task_state == AWAITING_RESPONSE


def default_name(project: str, number: int) -> str:
    """A slot's own derived label — the value `worktrees.name` holds when nobody has renamed it.

    The single home of this expression: every slot-declaring path (`app/routes/pool_slots.py`,
    `app/node_group_reconcile.py`) and `stored_opinion`, which recognises it as no opinion, must agree."""
    return f"{project}/{number}"


def stored_opinion(name: str | None, *, default_title: str | None, default_name: str) -> str | None:
    """The operator's opinion about this slot's label, or `None` when the stored `name` carries none.

    `name` is NOT NULL and backfilled with a derived label (`default_name` or the sensed
    `default_title`); without this every slot would present a derived label as an operator's choice,
    and a slot running a task would read `acme/8` instead of `1961|<the task>`.

    Derived spellings and blank map to `None`; everything else is returned verbatim, including a legacy
    stored `free` (`resolved_title` reads that one as absent beside an active task)."""
    stored = (name or "").strip()
    if not stored:
        return None
    # Latent hazard: `name` is inserted once from `default_title`, but `default_title` is a live sensed
    # column; if a node changes it, the frozen `name` becomes a fabricated opinion. Nothing sends one today.
    # The `default_name` arm is immune.
    if stored == (default_title or "").strip():
        return None
    if stored == default_name.strip():
        return None
    return name


# A stored `{digits}|…` Title is rule 3's own composite, never the operator's words.
_COMPOSITE_RE = re.compile(r"^\d+\|")


def operator_named(name: str | None, *, default_title: str | None, default_name: str) -> bool:
    """Whether `worktrees.name` holds the operator's own words — the one thing rule 3 must not overwrite.

    `routes/agent/slot_intents.py` parses the prior task id out of the stored Title, which an operator rename erases,
    so without this rule 3 would re-fire on the next pass and eat the rename.

    Not an opinion:
      * whatever `stored_opinion` calls absent (derived label, `default_title`, blank);
      * `free`, which rule 2 wrote before `name_to_store` stored the derived label for it (legacy rows
        must still be re-stampable);
      * a `{id}|…` composite, even a stale one — it is this module's own output."""
    opinion = stored_opinion(name, default_title=default_title, default_name=default_name)
    if opinion is None:
        return False
    stripped = opinion.strip()
    if stripped == FREE_TITLE:
        return False
    return _COMPOSITE_RE.match(stripped) is None


def resolved_title(
    stored: str | None,
    *,
    task_active: bool,
    task_id: int | None,
    task_title: str | None,
) -> str:
    """The Title every CP reader shows for one slot. NEVER returns `None` and never returns `""`.

    `stored` is `stored_opinion(...)`'s output, not `worktrees.name` raw. Three branches, first wins:

      1. `stored` — rendered verbatim.
      2. `{task_id}|{task_title}` — an active task but no stored Title.
      3. `FREE_TITLE`.

    `""` and NULL are the same absent value; emptiness is tested stripped, the return is raw. Branch 2
    requires the id and description, else it would emit a useless `"7|"`.

    A stored `free` beside an ACTIVE task is stale: `name_to_store` no longer writes it, but
    legacy rows still hold rule 2's word from before the lease, with rule 3's re-stamp never landed. It
    reads as absent then, or the console calls a slot running a task `free`."""
    if stored is not None and stored.strip() and not (task_active and stored.strip() == FREE_TITLE):
        return stored
    if task_active and task_id is not None and task_title is not None and task_title.strip():
        return f"{task_id}|{task_title}"
    return FREE_TITLE


# ── The write concern: what one declaration makes of `worktrees.name` (R3/R5/R10) ──────────────────────
#
# Rule 2 keys on `Worktree.state`; rule 3's `declared_task_id` is read off the same `worktrees` row
# `slot_state.lease()` bound `task_id` onto, so the caller needs no `task_id` on the wire.

# Characters that cannot survive the trip to a tmux window name; guarded where the value lands.
_INVALID_TITLE_CHARS = ("\n", "\r", "\0")

# Would collide with the console's window-name vocabulary: `console.py` tells an agent window from a
# shell by the `|term` suffix, so a slot titled `term` hides the agent window and re-opens the
# double-agent spawn. Matched case-insensitively.
_RESERVED_TITLE_SUFFIXES = ("|term", "|web")
_RESERVED_TITLES = ("term", "web")


class InvalidTitle(ValueError):
    """A Title that must not reach `worktrees.name`. Carries the reason, which every producer surfaces.

    Raised rather than sanitised: silently rewriting an operator's label is worse than refusing it."""


def invalid_title_reason(value: str) -> str | None:
    """Why `value` may not be stored as a Title, or `None` when it may be.

    One validator for both producers: `tasks.title` is free text, so a Task titled `term` composes to
    `42|term`."""
    if any(char in value for char in _INVALID_TITLE_CHARS):
        return "a title may not contain a newline, carriage return or NUL"
    folded = value.strip().lower()
    if folded in _RESERVED_TITLES:
        return f"{value!r} is reserved: it is how the console names a plain shell window"
    if folded.endswith(_RESERVED_TITLE_SUFFIXES):
        return (
            f"{value!r} ends in a reserved suffix ({'/'.join(_RESERVED_TITLE_SUFFIXES)}): the console "
            "discriminates an agent window from a shell one by it"
        )
    return None


def _checked(value: str) -> str:
    reason = invalid_title_reason(value)
    if reason is not None:
        raise InvalidTitle(reason)
    return value


def title_for_declaration(
    *,
    # `None` is a real argument: the edit route passes it with `declared_task_id=None`, making rule 3
    # unreachable from an edit.
    state: str | None,
    declared_task_id: int | None,
    prior_task_id: int | None,
    task_title: str | None,
    supplied_title: str | None,
    operator_named: bool = False,
) -> str | None:
    """What one declaration makes of this slot's Title. `None` = leave it. ORDERED, first match wins:

      1. `supplied_title is not None`            -> that value (`""` = an operator CLEAR)
      2. `state == free`                         -> `free`, ignoring everything else
      3. a task id that DIFFERS from the prior,
         and the operator has NOT named the slot -> `{declared_task_id}|{task_title}`
      -  else                                    -> `None`

    Last-writer-wins, no CAS: events are human-paced and a rejected write would wedge a lease over a
    cosmetic label. Rule 1 keys on the value, not `model_fields_set` (an unset title arrives as `null`).
    Rule 3 is a transition test, since `task_id` does not change after `lease()`; otherwise it would
    re-stamp every call. Raises `InvalidTitle` for whichever value it selected."""
    if supplied_title is not None:
        return _checked(supplied_title)
    # The caller reads `state` off the same row as `declared_task_id`, so a release reads `free` at once.
    if state == FREE:
        return FREE_TITLE
    # The second half of the transition test: `prior_task_id` is derived from the stored Title, which an
    # operator rename erases, so the id test alone would eat the rename.
    if operator_named:
        return None
    if declared_task_id is not None and declared_task_id != prior_task_id:
        # No description to compose with: leave it rather than store `42|None`; the read seam still resolves.
        if task_title is None or not task_title.strip():
            return None
        return _checked(f"{declared_task_id}|{task_title}")
    return None


def name_to_store(decided: str | None, *, default_name: str) -> str | None:
    """`title_for_declaration`'s verdict as it must land in the NOT NULL `name`. `None` = leave it.

    A clear (`""`) and rule 2's `free` both become the slot's derived label: `stored_opinion` reads it as
    absent, and a free slot holds no task (`ck_worktrees_state_ownership`), so `resolved_title` still
    shows `free`. Storing the word itself would outlive the next lease if rule 3's re-stamp never landed.
    Both write seams route through here."""
    if decided is None:
        return None
    stripped = decided.strip()
    return decided if stripped and stripped != FREE_TITLE else default_name


def title_for_declaration_fail_open(
    *,
    state: str | None,
    declared_task_id: int | None,
    prior_task_id: int | None,
    task_title: str | None,
    supplied_title: str | None,
    operator_named: bool = False,
) -> tuple[str | None, str | None]:
    """`POST /api/agent/slot-holder`'s policy on an invalid Title: `(title_to_write, rejection_reason)`.

    Fails open: drop the offending value, let the remaining rules run, and report it — never 422, since
    the request also carries `last_used`. `PUT .../title` takes the opposite policy (422, nothing
    written), as its request carries only the title. Dropping rule 1's value re-runs rules 2-3; if that
    also rejects, both reasons are reported and nothing is written."""
    # Spelled out rather than `**fields`, so a bad keyword is a type error, not a runtime `TypeError`.
    fields = dict(
        state=state,
        declared_task_id=declared_task_id,
        prior_task_id=prior_task_id,
        task_title=task_title,
        operator_named=operator_named,
    )
    reasons: list[str] = []
    try:
        return title_for_declaration(**fields, supplied_title=supplied_title), None
    except InvalidTitle as exc:
        reasons.append(str(exc))
    if supplied_title is None:
        return None, "; ".join(reasons)
    try:
        return title_for_declaration(**fields, supplied_title=None), "; ".join(reasons)
    except InvalidTitle as exc:
        reasons.append(str(exc))
        return None, "; ".join(reasons)
