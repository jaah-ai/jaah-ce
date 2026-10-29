"""Auto-title refinement: the LLM title a task gets after its create call has already returned.

The create stamps the deterministic `short_title(body)` (also the no-key/vendor-failure title); this
refinement replaces it and SSE carries the change to an open board.

The placeholder is also the guard: `refine_title` writes only while the row still carries exactly
`short_title(body)`, so a rename or authored title is never touched and a recovered run is a read.
Accepted gap: a description edit inside the window leaves that task on its placeholder.
Accepted gap: a link-only body created by a release before link-only titling (`short_title.link_only_title`)
carries the old placeholder, which no longer matches, so a refinement still queued across that deploy writes nothing.

The task's first image attachments go with a short text (`_image_rows`, `title_needs_images`), fetched after the
connection is released: a "fix this" body names its subject only in the screenshot.

A runner-filed task (`task_create`, `routes/tasks/agent_filing.py`) is stored with the filer's whole title
and refined the same way, from that title plus the body: its guard is the filer's title instead of the
placeholder (`refine_title(authored=)`), and its fallback is that title cut to the cap.

"Re-generate title" (`regenerated_title`) is the forced path: same model and prompt, no placeholder guard,
and the caller writes the result as a rename (`routes/tasks/title.py`).

Epic children titled `[<persona>] <old epic title>` are re-derived with it. Dispatch can beat this, so
`app/slot_retitle.py` relabels slot and tmux window afterwards (best effort); a prompt's `Title:` line
is not chased. No connection is held across the vendor call (pool ceiling; see `app/task_retag.py`).
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select, update

from app import attachment_storage, slot_retitle
from app.db import SessionLocal
from app.events import emit_console_event
from app.llm_client import IMAGE_TYPES, sniffed_image_type, summary_timeout
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task
from app.session_summary import title_from_description, title_needs_images
from app.short_title import short_title
from app.task_lifecycle import DRAFT
from app.tenancy import seam

log = logging.getLogger("cockpit-task-title")


#: The title an unsaved auto-save draft carries, verbatim, until Save. A constant rather than a
#: derivation, so a draft is not titled from its own first fragment on every editor tick. No refinement
#: is enqueued while a row is unsaved (`routes/tasks/dispatch.py`); both land at Save, in `_finalize_unsaved_draft`.
UNSAVED_DRAFT_TITLE = "New task"

#: Screenshots a title call is sent, the task's first ones: the subject of a "fix this" body is in them.
MAX_TITLE_IMAGES = 2
#: Each one's raw size cap, far under the vendor's 5 MB: the upload rides the title timeout, sized for a
#: ten-token reply. A typical screenshot is well under it; a larger one is skipped, not resized.
MAX_TITLE_IMAGE_BYTES = 1024 * 1024


def draft_title(task: Task) -> str:
    """The title Save will stamp on this row: `placeholder_title(body)` for an unsaved draft still wearing
    `UNSAVED_DRAFT_TITLE`, else the row's own title (an authored one, or a saved row's).

    The finalize's rule (`routes/tasks/dispatch.py::_finalize_unsaved_draft`), and what the board LISTS such a
    draft as, since its body already says what it is. The detail read keeps the constant: the editor
    must not take a derived title for an authored one."""
    if task.unsaved and task.title == UNSAVED_DRAFT_TITLE:
        return placeholder_title(task.body or "")
    return task.title


def placeholder_title(body: str) -> str:
    """The title a create stamps immediately — the deterministic derivation, no network.

    A named seam because the two create paths and `is_placeholder_title` must agree exactly; a divergent
    spelling makes the refinement silently never fire."""
    return short_title(body)


def is_placeholder_title(task: Task) -> bool:
    """Is this row still carrying the title create derived, rather than one somebody chose?

    Byte equality with `placeholder_title(body)`, not a heuristic; anything else is a deliberate choice
    and is never touched."""
    return task.title == placeholder_title(task.body or "")


def derived_child_title(persona: str, parent_title: str) -> str:
    """An epic child's stored title, `[<persona>] <epic title>`.

    Must match `_create_analysis_children` and the fan-out's finish child exactly, or no child matches;
    `apps/web`'s `RAW_PERSONA_PREFIX_RE` parses the prefix back off, so the shape is a contract."""
    return f"[{persona}] {parent_title}"


def refine_title(task_id: int, authored: str | None = None) -> bool:
    """Replace `task_id`'s placeholder title with the LLM-derived one. True when a title was written.

    `authored` is a filer's own title: the row is refined while it still carries exactly that, from the title
    and body together, falling back to the title cut to the cap (`short_title(prose=False)`).

    Idempotent: every early return is "nothing to do". Raises only on a database failure (the caller's
    retry); vendor failures come back as the deterministic fallback, i.e. no change."""
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task is None:
            # Deleted between the create and this run. Not an error: there is no title to fix.
            return False
        still_unchosen = is_placeholder_title(task) if authored is None else task.title == authored
        if task.archived_at is not None or not still_unchosen:
            return False
        # Snapshot, then release the connection. Read before the rollback, which expires `task`; the
        # locked re-read below is the only live handle afterwards.
        body = task.body or ""
        old_title = task.title
        image_rows = _image_rows(session, task_id)
        session.rollback()

        generated = _generated_title(body, image_rows, authored)
        if not generated or generated == old_title:
            # Fallback, empty reply, or the same title: the row already says this.
            return False

        locked = session.get(Task, task_id, with_for_update=True)
        if locked is None:
            return False
        # Re-checked under the lock against the snapshot, so a rename during the call wins.
        if locked.title != old_title:
            return False
        _retitle(session, task_id, generated)
        rederived = _rederive_children(session, locked, old_title, generated)
        # No durable ActivityEvent: a title changes no `state`, and a same-state `task:*` row would read
        # as a transition (`_epic_time_metrics`). The notify still refreshes an open board.
        emit_console_event(
            session, entity="task", id=task_id, action="titled", record_transition=False
        )
        # Which task, never the body (the provider and model are `title_from_description`'s line). No actor
        # on a DBOS thread; correlate with the `task:created` ActivityEvent.
        log.info("title: task %s retitled", task_id)
        session.commit()
        # After the commit and best effort: `retitle_task_slot` commits its own re-stamp and never
        # raises, so a dead node cannot cost a retry. Children too, since each holds its own slot.
        for retitled_id in (task_id, *rederived):
            slot_retitle.retitle_task_slot(session, retitled_id)
        return True


def regenerated_title(task_id: int) -> str | None:
    """The title the model gives `task_id`'s current body now, whatever the row carries — or None when it
    gives nothing new (deleted row, empty body, vendor failure, or the same title). Writes nothing.

    The forced path behind "Re-generate title" (`routes/tasks/title.py`): the placeholder guard is skipped
    because a person asked, and the caller writes the result as their own rename. The fallback is the row's
    current title, so a vendor failure reads as "no change", never as the placeholder over a chosen title.
    No connection is held across the vendor call, as in `refine_title`."""
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task is None:
            return None
        body = task.body or ""
        current = task.title
        image_rows = _image_rows(session, task_id)
        session.rollback()
    generated = _generated_title(body, image_rows, None, fallback=current)
    return generated if generated and generated != current else None


def _generated_title(
    body: str, image_rows: list[tuple[str, str | None]], authored: str | None, *, fallback: str | None = None
) -> str:
    """One call to the title model: from `body`, or from a filer's `authored` title and the body together.

    `fallback` is what a vendor failure returns: by default the deterministic derivation (`refine_title`'s
    "no change"); `authored` falls back to that title cut to the cap."""
    description = body if authored is None else f"{authored}\n\n{body}"
    # Not fetched for a description that names its own subject: the model would not be sent them.
    images = _fetch_images(image_rows) if body.strip() and title_needs_images(description) else []
    if authored is None:
        return title_from_description(body, fallback=fallback, images=images)
    return title_from_description(description, fallback=short_title(authored, prose=False), images=images)


def _image_rows(session, task_id: int) -> list[tuple[str, str | None]]:
    """`(s3_key, s3_bucket)` of the task's first confirmed image attachments the model may be sent, by id.

    The declared type and size only pre-filter; `_fetch_images` sniffs the bytes themselves."""
    rows = session.execute(
        select(TaskAttachment.s3_key, TaskAttachment.s3_bucket)
        .where(
            TaskAttachment.task_id == task_id,
            TaskAttachment.kind == ATTACHMENT_KIND_FILE,
            TaskAttachment.content_type.in_(IMAGE_TYPES),
            TaskAttachment.size_bytes <= MAX_TITLE_IMAGE_BYTES,
            TaskAttachment.uploaded_at.is_not(None),
            TaskAttachment.s3_key.is_not(None),
        )
        .order_by(TaskAttachment.id)
        .limit(MAX_TITLE_IMAGES)
    ).all()
    return [(key, bucket) for key, bucket in rows]


def _fetch_images(rows: list[tuple[str, str | None]]) -> list[tuple[str, bytes]]:
    """The bytes of `rows` as `(sniffed media type, bytes)`, all within one `summary_timeout()`. Best effort: an
    image that cannot be fetched in time, or whose bytes are no format the model reads, is left out and the
    title is made from the text alone — no failure here may cost the title, so every error is caught."""
    deadline = time.monotonic() + summary_timeout()
    images: list[tuple[str, bytes]] = []
    for key, bucket in rows:
        left = deadline - time.monotonic()
        if left < 1.0:
            break
        try:
            where = attachment_storage.target(bucket)
            if where is None:
                continue
            body = attachment_storage.fetch(where, key, timeout=left)
        except Exception as exc:  # noqa: BLE001 — a screenshot is optional; the title is not
            log.warning("title: image %s not fetched (%s: %s)", key, type(exc).__name__, exc)
            continue
        media_type = sniffed_image_type(body)
        if media_type is not None and len(body) <= MAX_TITLE_IMAGE_BYTES:
            images.append((media_type, body))
    return images


def _retitle(session, task_id: int, title: str) -> None:
    """Write one row's title without moving `tasks.updated_at`, and without stamping an actor.

    The pinned clock is load-bearing (precedent: `app/task_retag.py::mark_task_columns`): `apps/web`'s edit form is
    keyed on `${task.id}:${task.updated_at}`, so a bumped stamp remounts it and discards the operator's
    typing — likely here, seconds after a create. Assigning the column its own value beats `onupdate`.
    No `actors.task_actor_values(...)`, since the clock does not move; allowlisted by name in
    the write-site drift scanner. Cost: a form open across the window can save the
    placeholder back."""
    session.execute(
        update(Task).where(Task.id == task_id).values(title=title, updated_at=Task.updated_at)
    )


def _rederive_children(session, parent: Task, old_title: str, new_title: str) -> list[int]:
    """Re-stamp every direct child still carrying `[<persona>] <old parent title>`. Returns their ids.

    The return feeds the slot relabel, so the guard has one spelling. Archived children are kept
    evidence and skipped. Ordered by id for deterministic lock order. No event per child: the parent's
    notify already reloads boards and child pages."""
    children = session.scalars(
        select(Task)
        .where(Task.parent_task_id == parent.id, Task.archived_at.is_(None))
        .order_by(Task.id)
    ).all()
    rederived: list[int] = []
    for child in children:
        if child.title == derived_child_title(child.persona, old_title):
            _retitle(session, child.id, derived_child_title(child.persona, new_title))
            rederived.append(child.id)
    return rederived


def filed_titles_owed() -> list[tuple[int, str]]:
    """`(id, title)` of every filed draft still wearing its filer's over-cap title, untouched since filing.

    Untouched is `updated_at = created_at` (one insert stamps both; `_retitle` pins the clock): an operator's
    rename or a filer's amend moves it, so a title somebody chose since is never swept. Fleet-wide by subject;
    only ids and titles leave, and each refinement binds its own task's org."""
    with seam.operator_scope(), SessionLocal() as session:
        rows = session.execute(
            select(Task.id, Task.title).where(
                Task.state == DRAFT,
                Task.created_by_task_id.is_not(None),
                Task.archived_at.is_(None),
                Task.updated_at == Task.created_at,
            )
        ).all()
    return [(task_id, title) for task_id, title in rows if title != short_title(title, prose=False)]
