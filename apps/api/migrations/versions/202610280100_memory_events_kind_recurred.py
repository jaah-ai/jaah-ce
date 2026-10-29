"""`memory_events.kind` gains `recurred`: the failure an injected unit warned about happened anyway

A node-reported hook event; the nightly slice and the dispatch prompt rank a unit down by its recurred
share (`app/project_memory/events.py::recurred_factor`). The CHECK is widened, so every row and every write an older
release makes stays valid. The literal is frozen here, as the baseline's is: `tests/test_project_memory_schema.py`
pins it equal to `vocabulary.EVENT_KINDS`.

Revision ID: 202610280100
Revises: 202610280000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202610280100"
down_revision: Union[str, None] = "202610280000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BEFORE = "kind = ANY (ARRAY['injected'::text, 'read'::text, 'cited'::text, 'followed'::text])"
_AFTER = "kind = ANY (ARRAY['injected'::text, 'read'::text, 'cited'::text, 'followed'::text, 'recurred'::text])"


def _add_kind_check(expression: str) -> None:
    """`NOT VALID` then `VALIDATE`: the add takes a brief lock, the scan runs without blocking writes."""
    op.execute(f"ALTER TABLE memory_events ADD CONSTRAINT ck_memory_events_kind CHECK ({expression}) NOT VALID")
    op.execute("ALTER TABLE memory_events VALIDATE CONSTRAINT ck_memory_events_kind")


def upgrade() -> None:
    op.drop_constraint("ck_memory_events_kind", "memory_events", type_="check")
    _add_kind_check(_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM memory_events WHERE kind = 'recurred'")
    op.drop_constraint("ck_memory_events_kind", "memory_events", type_="check")
    _add_kind_check(_BEFORE)
