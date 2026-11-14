"""The daily Pipeline page's engine: where each item is, whether a step lets it through, and what a press does.

Modules: `membership.py` (`locate`: the one place an item's stage is computed), `gate.py` (`gate`: the one rule for
every step, and its Start, Merge, Move and sign-off seams), `board.py` (the `view` and `items` reads), `actions.py`
(a step's verbs on many items at once, and archive/restore), `events.py` (the `pipeline.changed` live event).
Routes: `app/routes/pipeline_board.py`. The pipeline's setup (stages, step settings) is `app/releases/pipeline.py`.
"""
from __future__ import annotations

from app.pipeline import events as _events

_events.install()
